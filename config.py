"""
config.py - Configurações centralizadas via variáveis de ambiente (.env)
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Carrega variáveis do arquivo .env
load_dotenv()

# ─── Caminhos base ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CREDENTIALS_FILE = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.getenv("GOOGLE_TOKEN_FILE", BASE_DIR / "token.json"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", BASE_DIR / "downloads"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
LOG_DIR = Path(os.getenv("LOG_DIR", BASE_DIR / "logs"))
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "processamentos.db"))

# Garante que os diretórios existam
for _dir in (DOWNLOAD_DIR, OUTPUT_DIR, LOG_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# ─── Gmail ────────────────────────────────────────────────────────────────────
GMAIL_LABEL = os.getenv("GMAIL_LABEL", "PROCESSAR")
GMAIL_MAX_RESULTS = int(os.getenv("GMAIL_MAX_RESULTS", "50"))

# ─── Google Drive ─────────────────────────────────────────────────────────────
DRIVE_ROOT_FOLDER_NAME = os.getenv("DRIVE_ROOT_FOLDER_NAME", "Documentos")

# ─── Conversão / PDF ─────────────────────────────────────────────────────────
SUPPORTED_EXTENSIONS = {
    ".pdf", ".xml",
    ".jpg", ".jpeg", ".png",
    ".xls", ".xlsx",
    ".doc", ".docx",
    ".htm", ".html",   # CORREÇÃO: adicionado .htm e .html
}
LIBREOFFICE_BIN = os.getenv("LIBREOFFICE_BIN", "soffice")

# ─── Scopes OAuth2 ───────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
]

# ─── Logging ─────────────────────────────────────────────────────────────────
# CORREÇÃO: converte a string do .env para o nível inteiro que o módulo logging exige
_LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL: int = getattr(logging, _LOG_LEVEL_STR, logging.INFO)
LOG_FILE = LOG_DIR / "processamento.log"

# ─── Classificação ───────────────────────────────────────────────────────────
CLASSIFICATION_KEYWORDS: dict[str, list[str]] = {
    "Nota Fiscal": ["nota fiscal", "nf ", "nfe", "danfe", "fatura"],
    "NF-e": ["nf-e", "nfe", "nota fiscal eletrônica", "xml nfe"],
    "Boleto": ["boleto", "linha digitável", "código de barras", "vencimento", "pagamento"],
    "Conta Cemig": ["cemig", "conta de energia", "energia elétrica", "kwh"],
    "Comprovante de Pagamento": [
        "comprovante", "pagamento efetuado", "transferência", "pix", "ted", "doc"
    ],
    "Contrato": ["contrato", "acordo", "termo", "aditivo", "rescisão", "locação"],
}
DEFAULT_CLASSIFICATION = "Outros"
