"""
pdf_merger.py - Mescla múltiplos PDFs em um único PDF consolidado.

Ordem de mesclagem dos anexos por categoria:
  1. Boleto
  2. RPA
  3. Nota Fiscal
  4. Contrato
  5. Recibo
  6. Comprovante de Pagamento
  7. Outros
  8. Email (corpo — sempre por último)
"""

import logging
from pathlib import Path

from pypdf import PdfWriter, PdfReader

logger = logging.getLogger(__name__)

# Ordem de prioridade por classificação (menor índice = aparece primeiro)
_ORDEM_CLASSIFICACAO: list[str] = [
    "Boleto",
    "RPA",
    "Nota Fiscal",
    "NF-e",
    "Contrato",
    "Recibo",
    "Comprovante de Pagamento",
    "Conta Cemig",
    "Outros",
    # "Email" é sempre inserido por último no código, não precisa constar aqui
]


def _prioridade(classificacao: str) -> int:
    """Retorna o índice de prioridade da classificação (menor = mais cedo)."""
    classif_lower = classificacao.lower()
    for i, cat in enumerate(_ORDEM_CLASSIFICACAO):
        if cat.lower() in classif_lower or classif_lower in cat.lower():
            return i
    return len(_ORDEM_CLASSIFICACAO)  # Desconhecido vai para o final (antes do email)


class AnexoPdf:
    """Representa um PDF de anexo com sua classificação."""

    def __init__(self, caminho: Path, classificacao: str = "Outros") -> None:
        self.caminho = caminho
        self.classificacao = classificacao

    def __repr__(self) -> str:
        return f"AnexoPdf({self.caminho.name!r}, {self.classificacao!r})"


class PdfMerger:
    """Consolida múltiplos PDFs em um único arquivo, respeitando ordem por categoria."""

    def mesclar(
        self,
        email_pdf: Path,
        anexos_pdf: list[Path],
        destino: Path,
        classificacoes_anexos: list[str] | None = None,
    ) -> Path:
        """
        Mescla o PDF do email com os PDFs dos anexos na ordem por categoria.

        Ordem final:
          Boleto → RPA → Nota Fiscal → Contrato → Recibo →
          Comprovante → Conta Cemig → Outros → Email

        Args:
            email_pdf:               PDF gerado a partir do corpo do email.
            anexos_pdf:              Lista de PDFs convertidos dos anexos.
            destino:                 Caminho de saída do PDF consolidado.
            classificacoes_anexos:   Classificação de cada anexo (mesma ordem de anexos_pdf).
                                     Se None, todos são tratados como "Outros".

        Returns:
            Caminho do PDF consolidado gerado.
        """
        # Garante lista de classificações do mesmo tamanho que os anexos
        if classificacoes_anexos is None:
            classificacoes_anexos = ["Outros"] * len(anexos_pdf)
        elif len(classificacoes_anexos) < len(anexos_pdf):
            classificacoes_anexos = list(classificacoes_anexos) + \
                ["Outros"] * (len(anexos_pdf) - len(classificacoes_anexos))

        # Cria objetos AnexoPdf e ordena por prioridade de categoria
        anexos: list[AnexoPdf] = [
            AnexoPdf(caminho=pdf, classificacao=classif)
            for pdf, classif in zip(anexos_pdf, classificacoes_anexos)
        ]

        anexos_ordenados = sorted(anexos, key=lambda a: _prioridade(a.classificacao))

        if anexos_ordenados:
            logger.info(
                "Ordem de mesclagem: %s → Email",
                " → ".join(a.classificacao for a in anexos_ordenados),
            )

        writer = PdfWriter()

        # 1. Anexos na ordem de categoria
        for anexo in anexos_ordenados:
            self._adicionar_pdf(writer, anexo.caminho, rotulo=f"[{anexo.classificacao}] {anexo.caminho.name}")

        # 2. Email por último
        self._adicionar_pdf(writer, email_pdf, rotulo="[Email] corpo do email")

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