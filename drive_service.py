"""
drive_service.py - Integração com a API do Google Drive.

Responsabilidades:
  • Autenticação OAuth2 (compartilha token com Gmail)
  • Criação da estrutura <PASTA_RAIZ>/ANO/MES/ no Drive
  • Upload do PDF consolidado com retry automático
  • Retorno do link de visualização público

A pasta raiz é definida em drive_config.json (gerenciado por folder_selector.py).
Se o arquivo não existir, usa DRIVE_ROOT_FOLDER_NAME do config.py como fallback.
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from config import (
    CREDENTIALS_FILE,
    DRIVE_ROOT_FOLDER_NAME,
    SCOPES,
    TOKEN_FILE,
)

logger = logging.getLogger(__name__)

MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_PDF = "application/pdf"

# Códigos HTTP que justificam retry (erros transitórios)
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # segundos (dobra a cada tentativa)


class DriveService:
    """Encapsula operações com a API do Google Drive."""

    def __init__(self) -> None:
        self._creds: Credentials | None = None
        self._service: Any = None
        self._autenticar()
        self._cache_pastas: dict[str, str] = {}  # chave → folder_id

    # ── Autenticação ──────────────────────────────────────────────────────────

    def _autenticar(self) -> None:
        """Reutiliza o token OAuth2 gerado pelo GmailService, se existir."""
        if TOKEN_FILE.exists():
            self._creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

        if not self._creds or not self._creds.valid:
            if self._creds and self._creds.expired and self._creds.refresh_token:
                logger.info("Renovando token de acesso do Drive...")
                self._creds.refresh(Request())
            else:
                if not CREDENTIALS_FILE.exists():
                    raise FileNotFoundError(
                        f"Arquivo de credenciais não encontrado: {CREDENTIALS_FILE}"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_FILE), SCOPES
                )
                self._creds = flow.run_local_server(port=0)

            TOKEN_FILE.write_text(self._creds.to_json())

        self._service = build("drive", "v3", credentials=self._creds)
        logger.info("Google Drive autenticado com sucesso.")

    # ── Expõe o service autenticado (usado pelo folder_selector) ─────────────

    @property
    def service(self) -> Any:
        """Retorna o objeto autenticado da API do Drive."""
        return self._service

    # ── Estrutura de Pastas ───────────────────────────────────────────────────

    def obter_ou_criar_pasta(self, nome: str, pai_id: str | None = None) -> str:
        """
        Retorna o ID de uma pasta existente ou cria uma nova.
        Usa cache interno para evitar chamadas repetidas à API.
        """
        chave_cache = f"{pai_id or 'root'}:{nome}"
        if chave_cache in self._cache_pastas:
            return self._cache_pastas[chave_cache]

        query_parts = [
            f"name = '{nome}'",
            f"mimeType = '{MIME_FOLDER}'",
            "trashed = false",
        ]
        if pai_id and pai_id != "root":
            query_parts.append(f"'{pai_id}' in parents")
        else:
            query_parts.append("'root' in parents")

        try:
            resultado = (
                self._service.files()
                .list(
                    q=" and ".join(query_parts),
                    spaces="drive",
                    fields="files(id, name)",
                )
                .execute()
            )
            arquivos = resultado.get("files", [])
            if arquivos:
                folder_id: str = arquivos[0]["id"]
                logger.debug("Pasta existente: '%s' (id=%s)", nome, folder_id)
            else:
                meta: dict[str, Any] = {
                    "name": nome,
                    "mimeType": MIME_FOLDER,
                }
                if pai_id and pai_id != "root":
                    meta["parents"] = [pai_id]
                pasta = self._service.files().create(body=meta, fields="id").execute()
                folder_id = pasta["id"]
                logger.info("Pasta criada: '%s' (id=%s)", nome, folder_id)

            self._cache_pastas[chave_cache] = folder_id
            return folder_id

        except HttpError as exc:
            logger.error("Erro ao criar/buscar pasta '%s': %s", nome, exc)
            raise

    def garantir_estrutura(self, data: datetime, raiz_id: str | None = None) -> str:
        """
        Garante a estrutura <RAIZ>/AAAA/MM/ e retorna o ID da pasta MÊS.

        Args:
            data:     Data do email, usada para definir ANO e MÊS.
            raiz_id:  ID da pasta raiz configurada pelo usuário via
                      folder_selector. Se None ou "root", usa
                      DRIVE_ROOT_FOLDER_NAME do config.py como fallback.

        Returns:
            ID da pasta de mês (onde o PDF será depositado).
        """
        ano = str(data.year)
        mes = f"{data.month:02d}"

        # Determina a pasta raiz: preferência ao ID configurado pelo usuário
        if raiz_id and raiz_id != "root":
            logger.debug("Usando pasta raiz configurada (id=%s)", raiz_id)
            pasta_raiz_id = raiz_id
        else:
            # Fallback: cria/usa a pasta pelo nome definido no config.py
            logger.debug(
                "Usando pasta raiz padrão '%s' (drive_config.json não encontrado).",
                DRIVE_ROOT_FOLDER_NAME,
            )
            pasta_raiz_id = self.obter_ou_criar_pasta(DRIVE_ROOT_FOLDER_NAME)

        ano_id = self.obter_ou_criar_pasta(ano, pai_id=pasta_raiz_id)
        mes_id = self.obter_ou_criar_pasta(mes, pai_id=ano_id)
        return mes_id

    # ── Upload com retry ──────────────────────────────────────────────────────

    def fazer_upload(self, caminho_pdf: Path, pasta_id: str) -> str:
        """
        Faz upload de um PDF para a pasta especificada.
        Retenta automaticamente em erros transitórios (429, 5xx).
        Retorna o link de visualização do arquivo no Google Drive.
        """
        meta: dict[str, Any] = {
            "name": caminho_pdf.name,
            "parents": [pasta_id],
        }

        delay = _RETRY_DELAY
        for tentativa in range(1, _MAX_RETRIES + 1):
            # Recria o MediaFileUpload a cada tentativa para evitar
            # estado corrompido em uploads retomáveis após falha.
            media = MediaFileUpload(str(caminho_pdf), mimetype=MIME_PDF, resumable=True)
            try:
                arquivo = (
                    self._service.files()
                    .create(body=meta, media_body=media, fields="id, webViewLink")
                    .execute()
                )
                link: str = arquivo.get("webViewLink", "")
                logger.info(
                    "Upload concluído: %s → %s",
                    caminho_pdf.name,
                    link or arquivo["id"],
                )
                return link

            except HttpError as exc:
                status = exc.resp.status if exc.resp else 0
                if status in _RETRY_STATUS and tentativa < _MAX_RETRIES:
                    logger.warning(
                        "Upload falhou (HTTP %d) — tentativa %d/%d. "
                        "Aguardando %.1fs...",
                        status, tentativa, _MAX_RETRIES, delay,
                    )
                    time.sleep(delay)
                    delay *= 2  # backoff exponencial
                else:
                    logger.error("Erro no upload de %s: %s", caminho_pdf.name, exc)
                    raise

        raise RuntimeError(f"Upload falhou após {_MAX_RETRIES} tentativas.")