"""
classifier.py - Classificação automática de documentos por palavras-chave.
"""

import logging
import re
import unicodedata

from config import CLASSIFICATION_KEYWORDS, DEFAULT_CLASSIFICATION

logger = logging.getLogger(__name__)

# Mapa completo de substituição de acentos (maiúsculas e minúsculas)
_ACENTO_MAP: list[tuple[str, str]] = [
    # Maiúsculas
    ("Á", "A"), ("À", "A"), ("Â", "A"), ("Ã", "A"), ("Ä", "A"),
    ("É", "E"), ("È", "E"), ("Ê", "E"), ("Ë", "E"),
    ("Í", "I"), ("Ì", "I"), ("Î", "I"), ("Ï", "I"),
    ("Ó", "O"), ("Ò", "O"), ("Ô", "O"), ("Õ", "O"), ("Ö", "O"),
    ("Ú", "U"), ("Ù", "U"), ("Û", "U"), ("Ü", "U"),
    ("Ç", "C"), ("Ñ", "N"),
    # Minúsculas
    ("á", "a"), ("à", "a"), ("â", "a"), ("ã", "a"), ("ä", "a"),
    ("é", "e"), ("è", "e"), ("ê", "e"), ("ë", "e"),
    ("í", "i"), ("ì", "i"), ("î", "i"), ("ï", "i"),
    ("ó", "o"), ("ò", "o"), ("ô", "o"), ("õ", "o"), ("ö", "o"),
    ("ú", "u"), ("ù", "u"), ("û", "u"), ("ü", "u"),
    ("ç", "c"), ("ñ", "n"),
]


class Classifier:
    """Classifica documentos com base em palavras-chave configuráveis."""

    def __init__(
        self,
        keywords: dict[str, list[str]] | None = None,
        default: str = DEFAULT_CLASSIFICATION,
    ) -> None:
        self.keywords = keywords or CLASSIFICATION_KEYWORDS
        self.default = default
        # Pré-compila os padrões para desempenho
        self._patterns: dict[str, re.Pattern[str]] = {
            categoria: re.compile(
                "|".join(re.escape(kw) for kw in kws),
                re.IGNORECASE | re.UNICODE,
            )
            for categoria, kws in self.keywords.items()
        }

    def classificar(self, texto: str) -> str:
        """
        Retorna a categoria mais provável para o texto fornecido.

        A primeira categoria cujas palavras-chave forem encontradas vence.
        A ordem do dicionário CLASSIFICATION_KEYWORDS define a prioridade.
        """
        texto_limpo = self._normalizar(texto)
        for categoria, pattern in self._patterns.items():
            if pattern.search(texto_limpo):
                logger.debug("Documento classificado como '%s'", categoria)
                return categoria
        logger.debug("Nenhuma categoria encontrada; usando '%s'", self.default)
        return self.default

    @staticmethod
    def _normalizar(texto: str) -> str:
        """Remove caracteres de controle e normaliza espaços."""
        return re.sub(r"\s+", " ", texto).strip()

    @staticmethod
    def _remover_acentos(texto: str) -> str:
        """
        Remove acentos de forma robusta usando unicodedata (CORREÇÃO:
        cobre maiúsculas, minúsculas e todos os caracteres Unicode).
        """
        normalizado = unicodedata.normalize("NFD", texto)
        return "".join(c for c in normalizado if unicodedata.category(c) != "Mn")

    def sanitizar_nome_arquivo(self, assunto: str) -> str:
        """
        Converte o assunto do email em um nome de arquivo seguro (sem espaços
        ou caracteres especiais, apenas maiúsculas e underscores).
        """
        # CORREÇÃO: usa unicodedata para cobrir todos os acentos antes de upper()
        nome = self._remover_acentos(assunto).upper()
        # Remove qualquer caractere não alfanumérico (exceto underscore)
        nome = re.sub(r"[^A-Z0-9]+", "_", nome)
        nome = nome.strip("_")
        return nome[:80]  # Limita o tamanho
