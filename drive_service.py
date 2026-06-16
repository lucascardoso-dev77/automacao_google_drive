"""
drive_service.py - Integração com a API do Google Drive.

Responsabilidades:
  • Autenticação OAuth2 (compartilha token com Gmail)
  • Criação da estrutura /Documentos/ANO/MES/
  • Upload do PDF consolidado com retry automático
  • Retorno do link de visualização público
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

    # ── Autenticação ─────────────────────────────────────────────────────────

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
        if pai_id:
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
                if pai_id:
                    meta["parents"] = [pai_id]
                pasta = self._service.files().create(body=meta, fields="id").execute()
                folder_id = pasta["id"]
                logger.info("Pasta criada: '%s' (id=%s)", nome, folder_id)

            self._cache_pastas[chave_cache] = folder_id
            return folder_id

        except HttpError as exc:
            logger.error("Erro ao criar/buscar pasta '%s': %s", nome, exc)
            raise

    def garantir_estrutura(self, data: datetime) -> str:
        """
        Garante a estrutura /Documentos/AAAA/MM/ e retorna o ID da pasta MÊS.
        """
        ano = str(data.year)
        mes = f"{data.month:02d}"

        raiz_id = self.obter_ou_criar_pasta(DRIVE_ROOT_FOLDER_NAME)
        ano_id = self.obter_ou_criar_pasta(ano, pai_id=raiz_id)
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
            # CORREÇÃO: recria o MediaFileUpload a cada tentativa para evitar
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

        # Nunca deveria chegar aqui, mas satisfaz o type checker
        raise RuntimeError(f"Upload falhou após {_MAX_RETRIES} tentativas.")
