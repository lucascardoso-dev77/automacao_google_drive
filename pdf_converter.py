"""
pdf_converter.py - Conversão de múltiplos formatos para PDF.

Suporte:
  • HTML / HTM → PDF  (ReportLab — renderização visual estilo Gmail impressão)
  • XML        → PDF  (ReportLab — renderização legível)
  • IMG        → PDF  (Pillow: JPG, JPEG, PNG)
  • DOCX/DOC/XLS/XLSX → PDF (LibreOffice headless)
  • PDF        → PDF  (cópia direta)
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
from datetime import datetime

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, Preformatted, SimpleDocTemplate, Spacer,
    Table, TableStyle, HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

from config import LIBREOFFICE_BIN

logger = logging.getLogger(__name__)

OcrHook = Callable[[Path], Path]

_OFFICE_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".odt", ".ods", ".ppt", ".pptx"}

# Cores do tema Gmail
_COR_CINZA_CLARO  = colors.HexColor("#f1f3f4")
_COR_CINZA_MEDIO  = colors.HexColor("#5f6368")
_COR_CINZA_ESCURO = colors.HexColor("#202124")
_COR_AZUL_LABEL   = colors.HexColor("#e8f0fe")
_COR_AZUL_TEXTO   = colors.HexColor("#1967d2")
_COR_BORDA        = colors.HexColor("#e0e0e0")
_COR_VERMELHO_GM  = colors.HexColor("#EA4335")


class _HtmlParaTexto(HTMLParser):
    """Extrai texto limpo de HTML, preservando quebras de linha em tags de bloco."""

    _BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self._partes: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tl = tag.lower()
        if tl in ("style", "script", "head"):
            self._skip = True
        if tl in self._BLOCK_TAGS:
            self._partes.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("style", "script", "head"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._partes.append(data)

    def get_text(self) -> str:
        texto = "".join(self._partes)
        texto = re.sub(r"\n{3,}", "\n\n", texto)
        return texto.strip()


class PdfConverter:
    """Converte diferentes formatos de arquivo para PDF."""

    def __init__(self, ocr_hook: "OcrHook | None" = None) -> None:
        self.ocr_hook = ocr_hook

    # ── Interface pública ─────────────────────────────────────────────────────

    def converter(self, origem: Path, destino_dir: Path | None = None) -> Path:
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

    def html_string_para_pdf(
        self,
        html: str,
        destino: Path,
        assunto: str = "",
        remetente: str = "",
        data_email: str = "",
        labels: list[str] | None = None,
        nomes_anexos: list[str] | None = None,
        message_id: str = "",
    ) -> Path:
        """
        Converte o corpo HTML do email para PDF com visual estilo Gmail impresso
        (Ctrl+P), incluindo cabeçalho, remetente, labels, corpo e rodapé.
        """
        logger.info("Convertendo HTML do email → %s (estilo Gmail)", destino.name)
        return self._email_para_pdf_gmail(
            html=html,
            destino=destino,
            assunto=assunto,
            remetente=remetente,
            data_email=data_email,
            labels=labels or [],
            nomes_anexos=nomes_anexos or [],
            message_id=message_id,
        )

    # ── Renderização estilo Gmail ─────────────────────────────────────────────

    @staticmethod
    def _email_para_pdf_gmail(
        html: str,
        destino: Path,
        assunto: str,
        remetente: str,
        data_email: str,
        labels: list[str],
        nomes_anexos: list[str],
        message_id: str,
    ) -> Path:
        """Gera PDF com visual fiel ao Gmail impresso via Ctrl+P."""

        # ── Extrai texto do HTML ──────────────────────────────────────────────
        parser = _HtmlParaTexto()
        parser.feed(html)
        corpo_texto = parser.get_text()
        if not corpo_texto.strip():
            corpo_texto = re.sub(r"<[^>]+>", " ", html)
            corpo_texto = re.sub(r"\s+", " ", corpo_texto).strip()

        # ── Formata data de impressão ─────────────────────────────────────────
        agora_str = datetime.now().strftime("%d/%m/%Y, %H:%M")

        # ── Estilos ───────────────────────────────────────────────────────────
        styles = getSampleStyleSheet()

        st_topo_data = ParagraphStyle(
            "TopoData",
            parent=styles["Normal"],
            fontSize=8,
            textColor=_COR_CINZA_MEDIO,
            alignment=TA_LEFT,
        )
        st_topo_titulo = ParagraphStyle(
            "TopoTitulo",
            parent=styles["Normal"],
            fontSize=8,
            textColor=_COR_CINZA_MEDIO,
            alignment=TA_CENTER,
        )
        st_topo_pagina = ParagraphStyle(
            "TopoPagina",
            parent=styles["Normal"],
            fontSize=8,
            textColor=_COR_CINZA_MEDIO,
            alignment=TA_RIGHT,
        )
        st_assunto = ParagraphStyle(
            "Assunto",
            parent=styles["Normal"],
            fontSize=18,
            textColor=_COR_CINZA_ESCURO,
            spaceAfter=6,
            fontName="Helvetica-Bold",
        )
        st_label = ParagraphStyle(
            "Label",
            parent=styles["Normal"],
            fontSize=8,
            textColor=_COR_AZUL_TEXTO,
            fontName="Helvetica-Bold",
        )
        st_remetente_nome = ParagraphStyle(
            "RemetenteNome",
            parent=styles["Normal"],
            fontSize=11,
            textColor=_COR_CINZA_ESCURO,
            fontName="Helvetica-Bold",
            spaceAfter=0,
        )
        st_remetente_email = ParagraphStyle(
            "RemetenteEmail",
            parent=styles["Normal"],
            fontSize=9,
            textColor=_COR_CINZA_MEDIO,
            spaceAfter=0,
        )
        st_para_mim = ParagraphStyle(
            "ParaMim",
            parent=styles["Normal"],
            fontSize=9,
            textColor=_COR_CINZA_MEDIO,
        )
        st_corpo = ParagraphStyle(
            "Corpo",
            parent=styles["Normal"],
            fontSize=10,
            leading=15,
            textColor=_COR_CINZA_ESCURO,
            spaceAfter=4,
        )
        st_anexo_titulo = ParagraphStyle(
            "AnexoTitulo",
            parent=styles["Normal"],
            fontSize=9,
            textColor=_COR_CINZA_MEDIO,
            fontName="Helvetica-Bold",
            spaceBefore=8,
            spaceAfter=4,
        )
        st_anexo_nome = ParagraphStyle(
            "AnexoNome",
            parent=styles["Normal"],
            fontSize=8,
            textColor=_COR_CINZA_ESCURO,
            alignment=TA_CENTER,
        )
        st_rodape = ParagraphStyle(
            "Rodape",
            parent=styles["Normal"],
            fontSize=7,
            textColor=_COR_CINZA_MEDIO,
            alignment=TA_LEFT,
        )

        # ── Parseia remetente ─────────────────────────────────────────────────
        match_nome = re.match(r'^"?([^"<]+)"?\s*<([^>]+)>', remetente)
        if match_nome:
            nome_rem = match_nome.group(1).strip()
            email_rem = match_nome.group(2).strip()
        else:
            nome_rem = remetente
            email_rem = ""

        # ── Monta o documento ─────────────────────────────────────────────────
        doc = SimpleDocTemplate(
            str(destino),
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=14 * mm,
            bottomMargin=14 * mm,
        )

        largura_util = A4[0] - 36 * mm
        story: list = []

        # ── 1. Barra de topo (data | título | página 1/1) ─────────────────────
        topo_data = Paragraph(agora_str, st_topo_data)
        topo_titulo = Paragraph("Gmail - Caixa de entrada", st_topo_titulo)
        topo_pag = Paragraph("1/1", st_topo_pagina)
        topo_table = Table(
            [[topo_data, topo_titulo, topo_pag]],
            colWidths=[largura_util * 0.3, largura_util * 0.4, largura_util * 0.3],
        )
        topo_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(topo_table)
        story.append(HRFlowable(width="100%", thickness=0.5, color=_COR_BORDA, spaceAfter=8))

        # ── 2. Assunto + labels ───────────────────────────────────────────────
        assunto_safe = (assunto or "(sem assunto)").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        story.append(Paragraph(assunto_safe, st_assunto))

        # Labels estilo Gmail (ex: "Caixa de entrada ×  PROCESSADO ×")
        if labels:
            labels_cells = []
            for lb in labels:
                lb_safe = lb.replace("&", "&amp;")
                cell = Paragraph(f'<font color="#1967d2">{lb_safe} ×</font>', st_label)
                labels_cells.append(cell)
            # Renderiza labels inline como tabela horizontal
            label_table = Table(
                [labels_cells],
                colWidths=[40 * mm] * len(labels_cells),
            )
            label_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), _COR_AZUL_LABEL),
                ("ROUNDEDCORNERS", [4]),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]))
            story.append(label_table)
            story.append(Spacer(1, 6))

        story.append(HRFlowable(width="100%", thickness=0.5, color=_COR_BORDA, spaceAfter=10))

        # ── 3. Cabeçalho do remetente ─────────────────────────────────────────
        # Avatar circular simulado (quadrado com inicial)
        inicial = (nome_rem[0] if nome_rem else "?").upper()
        avatar_style = ParagraphStyle(
            "Avatar",
            parent=styles["Normal"],
            fontSize=14,
            textColor=colors.white,
            fontName="Helvetica-Bold",
            alignment=TA_CENTER,
        )
        avatar_cell = Paragraph(f'<font color="white"><b>{inicial}</b></font>', avatar_style)

        # Bloco nome + email
        nome_safe = nome_rem.replace("&", "&amp;").replace("<", "&lt;")
        email_safe = email_rem.replace("&", "&amp;").replace("<", "&lt;")
        data_safe = data_email.replace("&", "&amp;")

        rem_nome_p = Paragraph(nome_safe, st_remetente_nome)
        rem_email_p = Paragraph(f"&lt;{email_safe}&gt;" if email_safe else "", st_remetente_email)
        rem_data_p = Paragraph(data_safe, st_remetente_email)
        rem_para_p = Paragraph("para mim", st_para_mim)

        rem_info = Table(
            [[rem_nome_p], [rem_email_p], [rem_data_p], [rem_para_p]],
            colWidths=[largura_util - 22 * mm],
        )
        rem_info.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ]))

        header_table = Table(
            [[avatar_cell, rem_info]],
            colWidths=[14 * mm, largura_util - 14 * mm],
        )
        header_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), _COR_VERMELHO_GM),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (0, 0), 6),
            ("BOTTOMPADDING", (0, 0), (0, 0), 6),
            ("LEFTPADDING", (0, 0), (0, 0), 4),
            ("RIGHTPADDING", (0, 0), (0, 0), 4),
            ("LEFTPADDING", (1, 0), (1, 0), 10),
            ("TOPPADDING", (1, 0), (1, 0), 2),
        ]))
        story.append(header_table)
        story.append(Spacer(1, 12))

        # ── 4. Corpo do email ─────────────────────────────────────────────────
        for linha in corpo_texto[:60_000].split("\n"):
            linha_strip = linha.strip()
            if linha_strip:
                linha_safe = (
                    linha_strip
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                story.append(Paragraph(linha_safe, st_corpo))
            else:
                story.append(Spacer(1, 5 * mm))

        # ── 5. Seção de anexos ────────────────────────────────────────────────
        if nomes_anexos:
            story.append(Spacer(1, 6))
            story.append(HRFlowable(width="100%", thickness=0.5, color=_COR_BORDA, spaceAfter=6))
            qtd = len(nomes_anexos)
            story.append(Paragraph(
                f"{qtd} anexo{'s' if qtd > 1 else ''} • Verificados pelo Gmail",
                st_anexo_titulo
            ))

            # Grade de miniaturas (caixas cinzas com nome do arquivo)
            COLUNAS = 3
            linhas_anexos = []
            linha_atual = []
            for nome in nomes_anexos:
                nome_safe = nome.replace("&", "&amp;").replace("<", "&lt;")
                ext_color = _COR_VERMELHO_GM  # ícone vermelho padrão

                miniatura = Table(
                    [[Paragraph(f'<font color="white"><b>{Path(nome).suffix.upper().lstrip(".")[:4]}</b></font>',
                                ParagraphStyle("Ext", parent=styles["Normal"], fontSize=9,
                                               textColor=colors.white, fontName="Helvetica-Bold",
                                               alignment=TA_CENTER))]],
                    colWidths=[28 * mm],
                    rowHeights=[18 * mm],
                )
                miniatura.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), ext_color),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("BOX", (0, 0), (-1, -1), 0.5, _COR_BORDA),
                ]))

                celula = Table(
                    [[miniatura], [Paragraph(nome_safe[:30], st_anexo_nome)]],
                    colWidths=[30 * mm],
                )
                celula.setStyle(TableStyle([
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]))

                linha_atual.append(celula)
                if len(linha_atual) == COLUNAS:
                    linhas_anexos.append(linha_atual)
                    linha_atual = []

            if linha_atual:
                # Preenche células vazias para completar a linha
                while len(linha_atual) < COLUNAS:
                    linha_atual.append(Paragraph("", styles["Normal"]))
                linhas_anexos.append(linha_atual)

            grade = Table(
                linhas_anexos,
                colWidths=[32 * mm] * COLUNAS,
            )
            grade.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(grade)

        # ── 6. Rodapé com URL ─────────────────────────────────────────────────
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width="100%", thickness=0.5, color=_COR_BORDA, spaceAfter=4))
        url = f"https://mail.google.com/mail/u/0/#inbox/{message_id}" if message_id else "https://mail.google.com"
        story.append(Paragraph(url, st_rodape))

        doc.build(story)
        logger.info("PDF estilo Gmail gerado: %s", destino)
        return destino

    # ── Conversores internos ──────────────────────────────────────────────────

    @staticmethod
    def _copiar_pdf(origem: Path, destino: Path) -> Path:
        shutil.copy2(origem, destino)
        return destino

    @staticmethod
    def _html_arquivo_para_pdf(origem: Path, destino: Path) -> Path:
        html = origem.read_text(encoding="utf-8", errors="replace")
        return PdfConverter._html_texto_para_pdf_simples(html, destino)

    @staticmethod
    def _html_texto_para_pdf_simples(html: str, destino: Path) -> Path:
        """Versão simples para arquivos HTML avulsos (não emails)."""
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
                paragrafo = paragrafo.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(paragrafo, corpo_style))
            else:
                story.append(Spacer(1, 4 * mm))
        if not story:
            story = [Paragraph("(email sem conteúdo de texto)", corpo_style)]
        doc.build(story)
        return destino

    @staticmethod
    def _xml_para_pdf(origem: Path, destino: Path) -> Path:
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
        with Image.open(origem) as img:
            rgb = img.convert("RGB")
            rgb.save(str(destino), "PDF", resolution=150)
        return destino

    @staticmethod
    def _office_para_pdf(origem: Path, destino: Path) -> Path:
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