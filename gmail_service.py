"""
gmail_service.py - Integração com a API do Gmail via OAuth2.
"""

import base64
import logging
import re
from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    CREDENTIALS_FILE,
    DOWNLOAD_DIR,
    GMAIL_LABEL,
    GMAIL_MAX_RESULTS,
    SCOPES,
    SUPPORTED_EXTENSIONS,
    TOKEN_FILE,
)

logger = logging.getLogger(__name__)


class EmailData:
    """Estrutura de dados que representa um email processado."""

    def __init__(
        self,
        message_id: str,
        assunto: str,
        remetente: str,
        data: str,
        corpo_html: str,
        corpo_texto: str,
        anexos: list[Path],
    ) -> None:
        self.message_id = message_id
        self.assunto = assunto
        self.remetente = remetente
        self.data = data
        self.corpo_html = corpo_html
        self.corpo_texto = corpo_texto
        self.anexos = anexos

    def __repr__(self) -> str:
        return (
            f"EmailData(id={self.message_id!r}, assunto={self.assunto!r}, "
            f"remetente={self.remetente!r}, anexos={len(self.anexos)})"
        )


class GmailService:
    """Encapsula todas as operações com a API do Gmail."""

    def __init__(self) -> None:
        self._creds: Credentials | None = None
        self._service: Any = None
        self._autenticar()

    # ── Autenticação ─────────────────────────────────────────────────────────

    def _autenticar(self) -> None:
        """Realiza autenticação OAuth2, salvando/atualizando o token."""
        if TOKEN_FILE.exists():
            self._creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

        if not self._creds or not self._creds.valid:
            if self._creds and self._creds.expired and self._creds.refresh_token:
                logger.info("Renovando token de acesso do Gmail...")
                self._creds.refresh(Request())
            else:
                if not CREDENTIALS_FILE.exists():
                    raise FileNotFoundError(
                        f"Arquivo de credenciais não encontrado: {CREDENTIALS_FILE}\n"
                        "Baixe o credentials.json no Google Cloud Console."
                    )
                logger.info("Iniciando fluxo OAuth2 para Gmail...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_FILE), SCOPES
                )
                self._creds = flow.run_local_server(port=0)

            TOKEN_FILE.write_text(self._creds.to_json())
            logger.info("Token salvo em %s", TOKEN_FILE)

        self._service = build("gmail", "v1", credentials=self._creds)
        logger.info("Gmail autenticado com sucesso.")

    # ── Leitura de Emails ─────────────────────────────────────────────────────

    def _obter_label_id(self, label_name: str) -> str | None:
        """Busca o ID de uma label pelo nome."""
        try:
            resultado = self._service.users().labels().list(userId="me").execute()
            for label in resultado.get("labels", []):
                if label["name"].upper() == label_name.upper():
                    return label["id"]
        except HttpError as exc:
            logger.error("Erro ao listar labels: %s", exc)
        return None

    def listar_mensagens_nao_processadas(self) -> list[str]:
        """
        Retorna os IDs dos emails na label PROCESSAR que ainda não foram
        movidos para a label PROCESSADO.
        """
        label_id = self._obter_label_id(GMAIL_LABEL)
        if not label_id:
            logger.warning("Label '%s' não encontrada no Gmail.", GMAIL_LABEL)
            return []

        try:
            response = (
                self._service.users()
                .messages()
                .list(
                    userId="me",
                    labelIds=[label_id],
                    maxResults=GMAIL_MAX_RESULTS,
                )
                .execute()
            )
            mensagens = response.get("messages", [])
            ids = [m["id"] for m in mensagens]
            logger.info("%d email(s) encontrado(s) na label '%s'.", len(ids), GMAIL_LABEL)
            return ids
        except HttpError as exc:
            logger.error("Erro ao listar mensagens: %s", exc)
            return []

    def obter_email(self, message_id: str) -> "EmailData | None":
        """Baixa e analisa um email pelo ID, incluindo seus anexos."""
        try:
            msg_raw = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="raw")
                .execute()
            )
            raw_data = base64.urlsafe_b64decode(msg_raw["raw"])
            email_msg: Message = message_from_bytes(raw_data)

            assunto = self._decodificar_cabecalho(email_msg.get("Subject", "(sem assunto)"))
            remetente = self._decodificar_cabecalho(email_msg.get("From", "desconhecido"))
            data = email_msg.get("Date", "")

            corpo_html, corpo_texto = self._extrair_corpo(email_msg)
            anexos = self._baixar_anexos(email_msg, message_id)

            return EmailData(
                message_id=message_id,
                assunto=assunto,
                remetente=remetente,
                data=data,
                corpo_html=corpo_html,
                corpo_texto=corpo_texto,
                anexos=anexos,
            )
        except HttpError as exc:
            logger.error("Erro ao obter email %s: %s", message_id, exc)
            return None

    # ── Corpo do Email ────────────────────────────────────────────────────────

    def _extrair_corpo(self, msg: Message) -> tuple[str, str]:
        """Extrai os corpos HTML e texto-puro do email."""
        html = ""
        texto = ""

        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                disp = str(part.get("Content-Disposition", ""))
                if "attachment" in disp:
                    continue
                # CORREÇÃO: checa payload antes de decodificar
                payload = part.get_payload(decode=True)
                if not isinstance(payload, bytes) or not payload:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if ct == "text/html" and not html:
                    html = decoded
                elif ct == "text/plain" and not texto:
                    texto = decoded
        else:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes) and payload:
                charset = msg.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    html = decoded
                else:
                    texto = decoded

        return html, texto

    # ── Anexos ────────────────────────────────────────────────────────────────

    def _baixar_anexos(self, msg: Message, message_id: str) -> list[Path]:
        """
        Salva todos os anexos suportados e retorna os caminhos.

        CORREÇÃO: deduplicação por Content-ID para evitar anexos inline
        duplicados (imagens embutidas no HTML que aparecem como anexos).
        """
        pasta = DOWNLOAD_DIR / message_id
        pasta.mkdir(parents=True, exist_ok=True)
        arquivos: list[Path] = []
        content_ids_vistos: set[str] = set()

        for part in msg.walk():
            # CORREÇÃO: só processa partes com Content-Disposition = attachment
            # ou que tenham nome de arquivo definido
            disp = str(part.get("Content-Disposition", ""))
            nome_arquivo = part.get_filename()

            if not nome_arquivo:
                continue

            nome_arquivo = self._decodificar_cabecalho(nome_arquivo)
            ext = Path(nome_arquivo).suffix.lower()

            if ext not in SUPPORTED_EXTENSIONS:
                logger.debug("Anexo ignorado (extensão não suportada): %s", nome_arquivo)
                continue

            # CORREÇÃO: pula imagens inline duplicadas pelo Content-ID
            content_id = part.get("Content-ID", "")
            if content_id:
                cid_limpo = content_id.strip("<>")
                if cid_limpo in content_ids_vistos:
                    logger.debug("Anexo duplicado (Content-ID já visto): %s", nome_arquivo)
                    continue
                content_ids_vistos.add(cid_limpo)

            dados = part.get_payload(decode=True)
            if not isinstance(dados, bytes) or not dados:
                logger.warning("Anexo '%s' sem dados, ignorando.", nome_arquivo)
                continue

            destino = pasta / nome_arquivo
            destino = self._resolver_colisao(destino)
            destino.write_bytes(dados)
            logger.info("Anexo salvo: %s (%d bytes)", destino, len(dados))
            arquivos.append(destino)

        return arquivos

    # ── Manipulação de Labels ─────────────────────────────────────────────────

    def mover_para_processado(self, message_id: str) -> None:
        """
        Remove a label PROCESSAR e aplica a label PROCESSADO (criando-a
        se necessário) para evitar reprocessamento.
        """
        label_processar_id = self._obter_label_id(GMAIL_LABEL)
        label_processado_id = self._garantir_label("PROCESSADO")

        body: dict[str, list[str]] = {
            "removeLabelIds": [],
            "addLabelIds": [],
        }
        if label_processar_id:
            body["removeLabelIds"].append(label_processar_id)
        if label_processado_id:
            body["addLabelIds"].append(label_processado_id)

        try:
            self._service.users().messages().modify(
                userId="me", id=message_id, body=body
            ).execute()
            logger.info("Email %s movido para PROCESSADO.", message_id)
        except HttpError as exc:
            logger.error("Erro ao mover email %s: %s", message_id, exc)

    def _garantir_label(self, nome: str) -> str | None:
        """Busca ou cria uma label e retorna seu ID."""
        label_id = self._obter_label_id(nome)
        if label_id:
            return label_id
        try:
            label = (
                self._service.users()
                .labels()
                .create(userId="me", body={"name": nome})
                .execute()
            )
            logger.info("Label '%s' criada com ID %s.", nome, label["id"])
            return label["id"]
        except HttpError as exc:
            logger.error("Erro ao criar label '%s': %s", nome, exc)
            return None

    # ── Utilitários ───────────────────────────────────────────────────────────

    @staticmethod
    def _decodificar_cabecalho(valor: str) -> str:
        """Decodifica cabeçalhos de email com codificações MIME."""
        from email.header import decode_header

        partes = decode_header(valor)
        resultado = []
        for parte, enc in partes:
            if isinstance(parte, bytes):
                resultado.append(parte.decode(enc or "utf-8", errors="replace"))
            else:
                resultado.append(parte)
        return "".join(resultado)

    @staticmethod
    def _resolver_colisao(caminho: Path) -> Path:
        """Adiciona sufixo numérico se o arquivo já existir."""
        if not caminho.exists():
            return caminho
        base = caminho.stem
        ext = caminho.suffix
        i = 1
        while True:
            novo = caminho.parent / f"{base}_{i}{ext}"
            if not novo.exists():
                return novo
            i += 1
