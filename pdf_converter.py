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

        # ── Formata data/hora de impressão ────────────────────────────────────
        agora_str = datetime.now().strftime("%d/%m/%Y, %H:%M")

        # ── Parseia remetente: "Nome Sobrenome <email@dominio>" ───────────────
        match_nome = re.match(r'^"?([^"<]+)"?\s*<([^>]+)>', remetente)
        if match_nome:
            nome_rem = match_nome.group(1).strip()
            email_rem = match_nome.group(2).strip()
        else:
            nome_rem = remetente.strip()
            email_rem = ""

        # ── Escolhe cor do avatar baseada na inicial (igual ao Gmail) ─────────
        _AVATAR_CORES = [
            "#1a73e8", "#d93025", "#1e8e3e", "#f29900",
            "#9334e6", "#007b83", "#c5221f", "#185abc",
        ]
        inicial = (nome_rem[0] if nome_rem else "?").upper()
        cor_avatar = colors.HexColor(
            _AVATAR_CORES[ord(inicial) % len(_AVATAR_CORES)]
        )

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
        styles = getSampleStyleSheet()

        # ── Estilos ───────────────────────────────────────────────────────────
        def _st(name, **kw):
            return ParagraphStyle(name, parent=styles["Normal"], **kw)

        st_topo      = _st("Topo",     fontSize=8,  textColor=_COR_CINZA_MEDIO)
        st_topo_c    = _st("TopoC",    fontSize=8,  textColor=_COR_CINZA_MEDIO, alignment=TA_CENTER)
        st_topo_r    = _st("TopoR",    fontSize=8,  textColor=_COR_CINZA_MEDIO, alignment=TA_RIGHT)
        st_assunto   = _st("Assunto",  fontSize=20, textColor=_COR_CINZA_ESCURO,
                           fontName="Helvetica-Bold", spaceAfter=6)
        st_label_txt = _st("LabelTxt", fontSize=8,  textColor=_COR_AZUL_TEXTO,
                           fontName="Helvetica-Bold")
        st_rem_nome  = _st("RemNome",  fontSize=11, textColor=_COR_CINZA_ESCURO,
                           fontName="Helvetica-Bold", spaceAfter=1)
        st_rem_sub   = _st("RemSub",   fontSize=9,  textColor=_COR_CINZA_MEDIO, spaceAfter=0)
        st_avatar    = _st("Avatar",   fontSize=15, textColor=colors.white,
                           fontName="Helvetica-Bold", alignment=TA_CENTER)
        st_corpo     = _st("Corpo",    fontSize=10, leading=15,
                           textColor=_COR_CINZA_ESCURO, spaceAfter=4)
        st_anx_hdr   = _st("AnxHdr",  fontSize=9,  textColor=_COR_CINZA_MEDIO,
                           fontName="Helvetica-Bold", spaceBefore=6, spaceAfter=6)
        st_anx_nome  = _st("AnxNome", fontSize=8,  textColor=_COR_CINZA_ESCURO,
                           alignment=TA_CENTER)
        st_anx_tipo  = _st("AnxTipo", fontSize=7,  textColor=colors.HexColor("#70757a"),
                           alignment=TA_CENTER)
        st_rodape    = _st("Rodape",  fontSize=7,  textColor=_COR_CINZA_MEDIO)

        story: list = []

        # ══════════════════════════════════════════════════════════════════════
        # 1. BARRA TOPO  →  "16/06/2026, 11:04   TESTE05 - email - Gmail   1/1"
        # ══════════════════════════════════════════════════════════════════════
        assunto_safe = (assunto or "(sem assunto)").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        email_rem_safe = email_rem.replace("&", "&amp;").replace("<", "&lt;")
        topo_centro = f"{assunto_safe} - {email_rem_safe} - Gmail" if email_rem_safe else f"{assunto_safe} - Gmail"

        topo = Table(
            [[Paragraph(agora_str, st_topo),
              Paragraph(topo_centro, st_topo_c),
              Paragraph("1/1", st_topo_r)]],
            colWidths=[largura_util * 0.25, largura_util * 0.50, largura_util * 0.25],
        )
        topo.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(topo)
        story.append(HRFlowable(width="100%", thickness=0.5, color=_COR_BORDA, spaceAfter=10))

        # ══════════════════════════════════════════════════════════════════════
        # 2. ASSUNTO  +  LABELS  (Caixa de entrada ×   PROCESSADO ×)
        # ══════════════════════════════════════════════════════════════════════
        story.append(Paragraph(assunto_safe, st_assunto))

        # Labels ao lado do assunto, estilo pill cinza
        labels_para_exibir = labels if labels else ["Caixa de entrada"]
        label_cells = []
        for lb in labels_para_exibir:
            lb_safe = lb.replace("&", "&amp;")
            p = Paragraph(f"{lb_safe} ×", st_label_txt)
            label_cells.append(p)

        col_w = min(50 * mm, largura_util / max(len(label_cells), 1))
        label_table = Table(
            [label_cells],
            colWidths=[col_w] * len(label_cells),
        )
        label_table.setStyle(TableStyle([
            ("BACKGROUND",     (0, 0), (-1, -1), _COR_AZUL_LABEL),
            ("TOPPADDING",     (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING",  (0, 0), (-1, -1), 3),
            ("LEFTPADDING",    (0, 0), (-1, -1), 7),
            ("RIGHTPADDING",   (0, 0), (-1, -1), 7),
            ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(label_table)
        story.append(Spacer(1, 8))
        story.append(HRFlowable(width="100%", thickness=0.5, color=_COR_BORDA, spaceAfter=10))

        # ══════════════════════════════════════════════════════════════════════
        # 3. BLOCO REMETENTE  →  [Avatar]  Nome Sobrenome
        #                                  <email@dominio>
        #                                  data
        #                                  para mim
        # ══════════════════════════════════════════════════════════════════════
        nome_safe  = nome_rem.replace("&", "&amp;").replace("<", "&lt;")
        data_safe  = data_email.replace("&", "&amp;")

        avatar_p = Paragraph(f"<b>{inicial}</b>", st_avatar)
        avatar_tbl = Table([[avatar_p]], colWidths=[12 * mm], rowHeights=[12 * mm])
        avatar_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), cor_avatar),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))

        info_rows = [[Paragraph(nome_safe, st_rem_nome)]]
        if email_rem_safe:
            info_rows.append([Paragraph(f"&lt;{email_rem_safe}&gt;", st_rem_sub)])
        if data_safe:
            info_rows.append([Paragraph(data_safe, st_rem_sub)])
        info_rows.append([Paragraph("para mim", st_rem_sub)])

        info_tbl = Table(info_rows, colWidths=[largura_util - 18 * mm])
        info_tbl.setStyle(TableStyle([
            ("TOPPADDING",    (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ]))

        rem_tbl = Table([[avatar_tbl, info_tbl]], colWidths=[14 * mm, largura_util - 14 * mm])
        rem_tbl.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (1, 0), (1, 0),   8),
            ("TOPPADDING",    (0, 0), (0, 0),   0),
            ("BOTTOMPADDING", (0, 0), (0, 0),   0),
        ]))
        story.append(rem_tbl)
        story.append(Spacer(1, 14))

        # ══════════════════════════════════════════════════════════════════════
        # 4. CORPO DO EMAIL
        # ══════════════════════════════════════════════════════════════════════
        linhas = corpo_texto[:80_000].split("\n")
        espacos_consecutivos = 0
        for linha in linhas:
            linha_strip = linha.strip()
            if linha_strip:
                espacos_consecutivos = 0
                linha_safe = (
                    linha_strip
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                story.append(Paragraph(linha_safe, st_corpo))
            else:
                espacos_consecutivos += 1
                if espacos_consecutivos <= 2:  # máx. 2 linhas em branco seguidas
                    story.append(Spacer(1, 4 * mm))

        # ══════════════════════════════════════════════════════════════════════
        # 5. SEÇÃO DE ANEXOS  →  "3 anexos • Verificados pelo Gmail"
        #    Grade: miniatura (preview colorido) + nome do arquivo + tipo
        # ══════════════════════════════════════════════════════════════════════
        if nomes_anexos:
            story.append(Spacer(1, 6))
            story.append(HRFlowable(width="100%", thickness=0.5, color=_COR_BORDA, spaceAfter=6))
            qtd = len(nomes_anexos)
            story.append(Paragraph(
                f"{qtd} anexo{'s' if qtd > 1 else ''} &nbsp;•&nbsp; Verificados pelo Gmail",
                st_anx_hdr,
            ))

            # Cores por extensão (fiel ao visual do Gmail)
            _EXT_CORES: dict[str, str] = {
                ".pdf":  "#EA4335",  # vermelho
                ".xml":  "#34A853",  # verde
                ".xls":  "#0F9D58",  # verde escuro
                ".xlsx": "#0F9D58",
                ".doc":  "#4285F4",  # azul
                ".docx": "#4285F4",
                ".jpg":  "#FBBC04",  # amarelo
                ".jpeg": "#FBBC04",
                ".png":  "#FBBC04",
            }

            COLUNAS = 3
            CARD_W  = 52 * mm
            CARD_H  = 36 * mm

            linhas_grade: list[list] = []
            linha_atual: list = []

            for nome_arq in nomes_anexos:
                ext = Path(nome_arq).suffix.lower()
                ext_label = ext.upper().lstrip(".")[:4] or "ARQ"
                cor_hex = _EXT_CORES.get(ext, "#5F6368")
                cor_card = colors.HexColor(cor_hex)

                nome_s = nome_arq.replace("&", "&amp;").replace("<", "&lt;")
                nome_curto = nome_s[:28] + ("…" if len(nome_s) > 28 else "")

                # Preview: retângulo colorido com extensão centralizada
                st_ext_label = ParagraphStyle(
                    f"ExtLbl_{ext_label}",
                    parent=styles["Normal"],
                    fontSize=11,
                    textColor=colors.white,
                    fontName="Helvetica-Bold",
                    alignment=TA_CENTER,
                )
                preview = Table(
                    [[Paragraph(f"<b>{ext_label}</b>", st_ext_label)]],
                    colWidths=[CARD_W - 4 * mm],
                    rowHeights=[CARD_H - 4 * mm],
                )
                preview.setStyle(TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, -1), cor_card),
                    ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                    ("BOX",           (0, 0), (-1, -1), 0.5, _COR_BORDA),
                ]))

                card = Table(
                    [[preview],
                     [Paragraph(nome_curto, st_anx_nome)],
                     [Paragraph(ext_label, st_anx_tipo)]],
                    colWidths=[CARD_W],
                )
                card.setStyle(TableStyle([
                    ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                    ("TOPPADDING",    (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
                ]))

                linha_atual.append(card)
                if len(linha_atual) == COLUNAS:
                    linhas_grade.append(linha_atual)
                    linha_atual = []

            # Completa última linha com células vazias
            if linha_atual:
                while len(linha_atual) < COLUNAS:
                    linha_atual.append(Paragraph("", styles["Normal"]))
                linhas_grade.append(linha_atual)

            grade = Table(linhas_grade, colWidths=[CARD_W] * COLUNAS)
            grade.setStyle(TableStyle([
                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
            ]))
            story.append(grade)

        # ══════════════════════════════════════════════════════════════════════
        # 6. RODAPÉ  →  URL do Gmail
        # ══════════════════════════════════════════════════════════════════════
        story.append(Spacer(1, 10))
        story.append(HRFlowable(width="100%", thickness=0.5, color=_COR_BORDA, spaceAfter=4))
        url = (
            f"https://mail.google.com/mail/u/0/#inbox/{message_id}"
            if message_id else "https://mail.google.com"
        )
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