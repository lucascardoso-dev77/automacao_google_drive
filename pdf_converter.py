"""
pdf_converter.py - Conversão de múltiplos formatos para PDF.

Suporte:
  • HTML / HTM → PDF  (ReportLab — sem dependências externas no Windows)
  • XML        → PDF  (ReportLab — renderização legível)
  • IMG        → PDF  (Pillow: JPG, JPEG, PNG)
  • DOCX/DOC/XLS/XLSX → PDF (LibreOffice headless)
  • PDF        → PDF  (cópia direta)

Nota: WeasyPrint foi removido pois requer bibliotecas GTK não disponíveis
no Windows por padrão. O ReportLab é usado como conversor HTML nativo.
"""

import logging
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Preformatted, SimpleDocTemplate, Spacer

from config import LIBREOFFICE_BIN

logger = logging.getLogger(__name__)

# Hook opcional de OCR
# Assinatura: (caminho_pdf_entrada: Path) -> Path  (retorna PDF com texto)
OcrHook = Callable[[Path], Path]

# Extensões que o LibreOffice converte
_OFFICE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods", ".ppt", ".pptx"}


class _HtmlParaTexto(HTMLParser):
    """Extrai texto limpo de HTML, preservando quebras de linha em tags de bloco."""

    _BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self._partes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self._partes.append("\n")

    def handle_data(self, data: str) -> None:
        self._partes.append(data)

    def get_text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._partes)).strip()


class PdfConverter:
    """Converte diferentes formatos de arquivo para PDF."""

    def __init__(self, ocr_hook: "OcrHook | None" = None) -> None:
        self.ocr_hook = ocr_hook

    # ── Interface pública ─────────────────────────────────────────────────────

    def converter(self, origem: Path, destino_dir: Path | None = None) -> Path:
        """
        Converte *origem* para PDF e salva no *destino_dir* (ou mesmo dir).
        Retorna o caminho do PDF gerado.
        """
        destino_dir = destino_dir or origem.parent
        destino_dir.mkdir(parents=True, exist_ok=True)
        destino = destino_dir / (origem.stem + ".pdf")

        ext = origem.suffix.lower()
        logger.info("Convertendo %s → %s", origem.name, destino.name)

        if ext == ".pdf":
            destino = self._copiar_pdf(origem, destino)
        elif ext in {".html", ".htm"}:
            destino = self._html_arquivo_para_pdf(origem, destino)
        elif ext == ".xml":
            destino = self._xml_para_pdf(origem, destino)
        elif ext in {".jpg", ".jpeg", ".png"}:
            destino = self._imagem_para_pdf(origem, destino)
        elif ext in _OFFICE_EXTS:
            destino = self._office_para_pdf(origem, destino)
        else:
            raise ValueError(f"Extensão não suportada para conversão: '{ext}'")

        if self.ocr_hook:
            logger.info("Aplicando OCR em %s...", destino.name)
            destino = self.ocr_hook(destino)

        return destino

    def html_string_para_pdf(self, html: str, destino: Path) -> Path:
        """
        Converte uma string HTML para PDF via ReportLab.
        Funciona nativamente no Windows sem dependências externas.
        """
        logger.info("Convertendo HTML do email → %s", destino.name)
        return self._html_texto_para_pdf(html, destino)

    # ── Conversores internos ──────────────────────────────────────────────────

    @staticmethod
    def _copiar_pdf(origem: Path, destino: Path) -> Path:
        shutil.copy2(origem, destino)
        return destino

    @staticmethod
    def _html_arquivo_para_pdf(origem: Path, destino: Path) -> Path:
        """Lê arquivo HTML e converte para PDF via ReportLab."""
        html = origem.read_text(encoding="utf-8", errors="replace")
        return PdfConverter._html_texto_para_pdf(html, destino)

    @staticmethod
    def _html_texto_para_pdf(html: str, destino: Path) -> Path:
        """
        Extrai texto do HTML e gera PDF formatado via ReportLab.
        Preserva quebras de parágrafo das tags de bloco.
        """
        parser = _HtmlParaTexto()
        parser.feed(html)
        texto = parser.get_text()

        if not texto.strip():
            texto = re.sub(r"<[^>]+>", " ", html)
            texto = re.sub(r"\s+", " ", texto).strip()

        doc = SimpleDocTemplate(
            str(destino),
            pagesize=A4,
            leftMargin=20 * mm,
            rightMargin=20 * mm,
            topMargin=25 * mm,
            bottomMargin=25 * mm,
        )
        styles = getSampleStyleSheet()
        corpo_style = ParagraphStyle(
            "Corpo",
            parent=styles["Normal"],
            fontSize=10,
            leading=14,
            spaceAfter=6,
        )

        story: list = []
        for paragrafo in texto[:50_000].split("\n"):
            paragrafo = paragrafo.strip()
            if paragrafo:
                # Escapa caracteres especiais do ReportLab
                paragrafo = (
                    paragrafo
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                story.append(Paragraph(paragrafo, corpo_style))
            else:
                story.append(Spacer(1, 4 * mm))

        if not story:
            story = [Paragraph("(email sem conteúdo de texto)", corpo_style)]

        doc.build(story)
        return destino

    @staticmethod
    def _xml_para_pdf(origem: Path, destino: Path) -> Path:
        """Gera um PDF legível a partir de XML, com formatação básica."""
        try:
            tree = ET.parse(origem)
            xml_formatado = ET.tostring(tree.getroot(), encoding="unicode")
        except ET.ParseError:
            xml_formatado = origem.read_text(errors="replace")

        doc = SimpleDocTemplate(
            str(destino),
            pagesize=A4,
            leftMargin=15 * mm,
            rightMargin=15 * mm,
            topMargin=20 * mm,
            bottomMargin=20 * mm,
        )
        styles = getSampleStyleSheet()
        titulo_style = ParagraphStyle(
            "Titulo",
            parent=styles["Heading1"],
            textColor=colors.HexColor("#1a3c5e"),
            spaceAfter=6,
        )
        code_style = ParagraphStyle(
            "Code",
            parent=styles["Code"],
            fontSize=7,
            leading=10,
            textColor=colors.HexColor("#2c2c2c"),
        )
        story = [
            Paragraph(f"Arquivo XML: {origem.name}", titulo_style),
            Spacer(1, 4 * mm),
            Preformatted(xml_formatado[:50_000], code_style),
        ]
        doc.build(story)
        return destino

    @staticmethod
    def _imagem_para_pdf(origem: Path, destino: Path) -> Path:
        """Converte imagem (JPG/JPEG/PNG) para PDF via Pillow."""
        with Image.open(origem) as img:
            rgb = img.convert("RGB")
            rgb.save(str(destino), "PDF", resolution=150)
        return destino

    @staticmethod
    def _office_para_pdf(origem: Path, destino: Path) -> Path:
        """
        Usa LibreOffice headless para converter DOC/DOCX/XLS/XLSX etc.
        No Windows, instale o LibreOffice em: https://www.libreoffice.org/download/
        """
        with tempfile.TemporaryDirectory(prefix="lo_convert_") as tmp:
            tmp_path = Path(tmp)
            try:
                resultado = subprocess.run(
                    [
                        LIBREOFFICE_BIN,
                        "--headless",
                        "--norestore",
                        "--convert-to", "pdf",
                        "--outdir", tmp,
                        str(origem),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except FileNotFoundError:
                raise RuntimeError(
                    f"LibreOffice não encontrado em '{LIBREOFFICE_BIN}'. "
                    "Baixe em: https://www.libreoffice.org/download/"
                )

            if resultado.returncode != 0:
                raise RuntimeError(
                    f"LibreOffice retornou erro (código {resultado.returncode}): "
                    f"{resultado.stderr.strip()}"
                )

            pdf_tmp = tmp_path / (origem.stem + ".pdf")
            if not pdf_tmp.exists():
                raise FileNotFoundError(
                    f"LibreOffice não gerou PDF em '{tmp}'. "
                    f"Saída: {resultado.stdout.strip()}"
                )

            shutil.move(str(pdf_tmp), str(destino))
            return destino
