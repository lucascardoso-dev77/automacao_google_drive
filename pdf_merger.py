"""
pdf_merger.py - Mescla múltiplos PDFs em um único PDF consolidado.

Estrutura do PDF final:
  Página 1+    → Conteúdo completo do email (HTML → PDF)
  Páginas seg. → Anexos convertidos para PDF (ordem original)
"""

import logging
from pathlib import Path

from pypdf import PdfWriter, PdfReader

logger = logging.getLogger(__name__)


class PdfMerger:
    """Consolida múltiplos PDFs em um único arquivo."""

    def mesclar(
        self,
        email_pdf: Path,
        anexos_pdf: list[Path],
        destino: Path,
    ) -> Path:
        """
        Mescla o PDF do email com os PDFs dos anexos na ordem fornecida.

        Args:
            email_pdf:   PDF gerado a partir do corpo do email.
            anexos_pdf:  Lista de PDFs convertidos dos anexos (ordem original).
            destino:     Caminho de saída do PDF consolidado.

        Returns:
            Caminho do PDF consolidado gerado.
        """
        writer = PdfWriter()

        # 1. Corpo do email
        self._adicionar_pdf(writer, email_pdf, rotulo="email")

        # 2. Anexos na ordem original
        for caminho in anexos_pdf:
            self._adicionar_pdf(writer, caminho, rotulo=caminho.name)

        destino.parent.mkdir(parents=True, exist_ok=True)
        with open(destino, "wb") as f:
            writer.write(f)

        total = sum(1 for _ in PdfReader(destino).pages)
        logger.info(
            "PDF consolidado gerado: %s (%d páginas, %d anexo(s))",
            destino.name,
            total,
            len(anexos_pdf),
        )
        return destino

    @staticmethod
    def _adicionar_pdf(writer: PdfWriter, caminho: Path, rotulo: str = "") -> None:
        """Adiciona todas as páginas de um PDF ao writer."""
        if not caminho.exists():
            logger.warning("PDF não encontrado, ignorando: %s", caminho)
            return
        try:
            reader = PdfReader(str(caminho))
            for pagina in reader.pages:
                writer.add_page(pagina)
            logger.debug(
                "Adicionado '%s' (%d págs.)", rotulo or caminho.name, len(reader.pages)
            )
        except Exception as exc:
            logger.error("Erro ao ler PDF '%s': %s — ignorando.", caminho.name, exc)
