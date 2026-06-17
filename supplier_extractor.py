"""
supplier_extractor.py - Identificação automática do fornecedor/emitente do documento.

Hierarquia de prioridade:
  1. XML de NF-e  → tag <xNome> dentro do grupo <emit>
  2. PDF DANFE    → campos de Razão Social / Emitente no texto
  3. PDF Boleto   → campos de Cedente / Beneficiário no texto
  4. Fallback     → "DOCUMENTO_NAO_IDENTIFICADO"

Funções públicas:
  detectar_xml()               → retorna o primeiro .xml da lista, ou None
  extrair_fornecedor_xml()     → lê xNome do emit na NF-e
  extrair_fornecedor_nfe_pdf() → lê emitente em PDFs tipo DANFE
  extrair_fornecedor_boleto()  → lê cedente/beneficiário em PDFs de boleto
  sanitizar_nome_arquivo()     → limpa e normaliza o nome para uso em arquivo
  gerar_nome_final()           → orquestra tudo e devolve "DD-MM-AAAA - NOME.pdf"
"""

import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Constantes ────────────────────────────────────────────────────────────────

FALLBACK_NOME = "DOCUMENTO_NAO_IDENTIFICADO"
LIMITE_NOME = 80  # caracteres máximos para o nome do fornecedor

# Namespaces comuns da NF-e (versões 3.x e 4.x)
_NFE_NAMESPACES = [
    "http://www.portalfiscal.inf.br/nfe",
    "http://www.portalfiscal.inf.br/nfe/wsdl/NfeRecepcao2",
    "",  # sem namespace (fallback)
]

# Padrões para identificar Razão Social em PDFs de DANFE / Nota Fiscal
_PADROES_NF = [
    # "RAZÃO SOCIAL   EMPRESA EXEMPLO LTDA"
    r"RAZ[AÃ]O\s+SOCIAL[\s:]+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{3,})",
    # "NOME / RAZÃO SOCIAL   EMPRESA"
    r"NOME\s*/?\s*RAZ[AÃ]O\s+SOCIAL[\s:]+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{3,})",
    # "EMITENTE   EMPRESA EXEMPLO LTDA"
    r"EMITENTE[\s:]+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{3,})",
    # "FORNECEDOR   EMPRESA EXEMPLO LTDA"
    r"FORNECEDOR[\s:]+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{3,})",
    # Nome logo após CNPJ do emitente (padrão DANFE)
    r"CNPJ\s*[:\s]?\d{2}[.\s]?\d{3}[.\s]?\d{3}[/\s]?\d{4}[-\s]?\d{2}\s+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{5,})",
]

# Padrões para identificar Cedente / Beneficiário em boletos
_PADROES_BOLETO = [
    r"CEDENTE[\s:]+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{3,})",
    r"BENEFICI[AÁ]RIO[\s:]+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{3,})",
    r"BENEFICI[AÁ]RIO\s+FINAL[\s:]+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{3,})",
    r"RECEBEDOR[\s:]+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{3,})",
    r"FAVORECIDO[\s:]+([A-ZÀ-Ú][A-ZÀ-Ú0-9 .,'&/-]{3,})",
]

# Palavras que indicam que o texto extraído é lixo (cabeçalho de coluna etc.)
_PALAVRAS_DESCARTE = {
    "DATA", "VALOR", "VENCIMENTO", "BANCO", "AGENCIA", "CONTA",
    "NUMERO", "PAGADOR", "SACADO", "DOCUMENTO", "LOCAL", "PAGAMENTO",
    "NOTA", "FISCAL", "SERIE", "FOLHA", "PAGINA",
}


# ── Utilitário interno ────────────────────────────────────────────────────────

def _remover_acentos(texto: str) -> str:
    """Remove diacríticos via NFD + filtro de categoria Mn."""
    return "".join(
        c for c in unicodedata.normalize("NFD", texto)
        if unicodedata.category(c) != "Mn"
    )


def _extrair_texto_pdf(caminho_pdf: Path) -> str:
    """
    Extrai texto de um PDF usando pdfplumber (preferido) ou PyMuPDF (fallback).
    Retorna string vazia em caso de falha, sem lançar exceção.
    """
    texto = ""

    # Tenta pdfplumber primeiro
    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(str(caminho_pdf)) as pdf:
            partes = []
            for pagina in pdf.pages:
                t = pagina.extract_text()
                if t:
                    partes.append(t)
            texto = "\n".join(partes)
        if texto.strip():
            logger.debug("Texto extraído via pdfplumber: %d chars", len(texto))
            return texto
    except ImportError:
        logger.debug("pdfplumber não disponível; tentando PyMuPDF.")
    except Exception as exc:
        logger.warning("pdfplumber falhou em '%s': %s", caminho_pdf.name, exc)

    # Fallback: PyMuPDF (fitz)
    try:
        import fitz  # type: ignore  # noqa: F401 (PyMuPDF)
        doc = fitz.open(str(caminho_pdf))
        partes = [pagina.get_text() for pagina in doc]
        doc.close()
        texto = "\n".join(partes)
        if texto.strip():
            logger.debug("Texto extraído via PyMuPDF: %d chars", len(texto))
            return texto
    except ImportError:
        logger.debug("PyMuPDF (fitz) não disponível.")
    except Exception as exc:
        logger.warning("PyMuPDF falhou em '%s': %s", caminho_pdf.name, exc)

    logger.warning(
        "Nenhuma biblioteca de extração de PDF disponível ou nenhum texto "
        "extraído de '%s'. Instale pdfplumber ou PyMuPDF.",
        caminho_pdf.name,
    )
    return ""


def _buscar_padroes(texto: str, padroes: list[str]) -> str | None:
    """
    Testa uma lista de padrões regex no texto (uppercase) e retorna o primeiro
    grupo capturado que não seja uma palavra de descarte.
    """
    texto_upper = texto.upper()
    for padrao in padroes:
        for match in re.finditer(padrao, texto_upper, re.MULTILINE):
            candidato = match.group(1).strip()
            # Remove possíveis valores de colunas adjacentes (pega até nova linha)
            candidato = candidato.split("\n")[0].strip()
            # Limita ao que parece um nome de empresa razoável
            candidato = re.sub(r"\s{2,}", " ", candidato).strip()
            if len(candidato) < 4:
                continue
            primeira_palavra = candidato.split()[0].upper()
            if primeira_palavra in _PALAVRAS_DESCARTE:
                continue
            logger.debug("Padrão '%s' → candidato: %r", padrao[:40], candidato)
            return candidato
    return None


# ── Funções públicas ──────────────────────────────────────────────────────────

def detectar_xml(anexos: list[Path]) -> Path | None:
    """
    Retorna o primeiro arquivo .xml encontrado na lista de anexos, ou None.

    Args:
        anexos: Lista de caminhos de arquivos baixados do email.

    Returns:
        Path do XML se encontrado, None caso contrário.
    """
    for anexo in anexos:
        if anexo.suffix.lower() == ".xml":
            logger.debug("XML detectado: %s", anexo.name)
            return anexo
    return None


def extrair_fornecedor_xml(xml_path: Path) -> str | None:
    """
    Extrai a Razão Social do emitente (tag <xNome> dentro de <emit>) de um
    XML de NF-e, suportando versões com e sem namespace.

    Args:
        xml_path: Caminho do arquivo XML.

    Returns:
        Razão social em maiúsculas, ou None se não encontrada.
    """
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as exc:
        logger.warning("XML malformado '%s': %s", xml_path.name, exc)
        return None

    for ns in _NFE_NAMESPACES:
        prefixo = f"{{{ns}}}" if ns else ""

        # Busca direta: //emit/xNome
        for emit in root.iter(f"{prefixo}emit"):
            xnome = emit.find(f"{prefixo}xNome")
            if xnome is not None and xnome.text:
                nome = xnome.text.strip()
                logger.info("Fornecedor extraído do XML (emit/xNome): %r", nome)
                return nome

        # Busca alternativa em todo o documento (NFe aninhada em nfeProc)
        for xnome in root.iter(f"{prefixo}xNome"):
            # Verifica se o pai é <emit>
            parent_tag = xnome.tag  # não temos parent direto no ElementTree básico
            # Estratégia: pega a primeira ocorrência de xNome (geralmente é o emitente)
            if xnome.text and xnome.text.strip():
                nome = xnome.text.strip()
                logger.info(
                    "Fornecedor extraído do XML (primeira xNome): %r", nome
                )
                return nome

    logger.warning("Tag <xNome> não encontrada no XML '%s'.", xml_path.name)
    return None


def extrair_fornecedor_nfe_pdf(pdf_path: Path) -> str | None:
    """
    Extrai o nome do emitente de um PDF de Nota Fiscal / DANFE buscando
    padrões de Razão Social, Emitente e Fornecedor no texto.

    Args:
        pdf_path: Caminho do PDF da nota fiscal.

    Returns:
        Nome do emitente em maiúsculas, ou None se não encontrado.
    """
    texto = _extrair_texto_pdf(pdf_path)
    if not texto:
        return None

    resultado = _buscar_padroes(texto, _PADROES_NF)
    if resultado:
        logger.info(
            "Fornecedor extraído do PDF NF-e '%s': %r", pdf_path.name, resultado
        )
    return resultado


def extrair_fornecedor_boleto(pdf_path: Path) -> str | None:
    """
    Extrai o nome do Cedente / Beneficiário de um PDF de boleto bancário.

    Args:
        pdf_path: Caminho do PDF do boleto.

    Returns:
        Nome do cedente em maiúsculas, ou None se não encontrado.
    """
    texto = _extrair_texto_pdf(pdf_path)
    if not texto:
        return None

    resultado = _buscar_padroes(texto, _PADROES_BOLETO)
    if resultado:
        logger.info(
            "Cedente/Beneficiário extraído do boleto '%s': %r",
            pdf_path.name,
            resultado,
        )
    return resultado


def sanitizar_nome_arquivo(nome: str) -> str:
    """
    Converte um nome de empresa em string segura para uso em nome de arquivo:
      • Remove acentos (unicodedata)
      • Converte para MAIÚSCULAS
      • Substitui caracteres inválidos (barras, dois-pontos, aspas, etc.) por espaço
      • Colapsa múltiplos espaços em um único
      • Faz strip e limita o tamanho

    Args:
        nome: Nome bruto do fornecedor.

    Returns:
        Nome sanitizado, pronto para uso em nome de arquivo.
    """
    nome = _remover_acentos(nome).upper()

    # Remove caracteres proibidos em nomes de arquivo (Windows + Unix)
    # Mantém: letras, dígitos, espaços, ponto, hífen, apóstrofo, &
    nome = re.sub(r'[\\/:*?"<>|,;!@#$%^()+=\[\]{}`~]', " ", nome)

    # Colapsa múltiplos espaços e hifens em espaço único
    nome = re.sub(r"\s+", " ", nome).strip()

    # Limita o tamanho (considera margem para a data e extensão)
    return nome[:LIMITE_NOME].strip()


def gerar_nome_final(
    data_email: datetime,
    anexos_originais: list[Path],
    pdfs_convertidos: list[Path] | None = None,
) -> str:
    """
    Orquestra a identificação do fornecedor e gera o nome final do arquivo
    no formato "DD-MM-AAAA - NOME_FORNECEDOR.pdf".

    Hierarquia:
      1. XML (xNome do emit)
      2. PDF de NF-e/DANFE
      3. PDF de Boleto
      4. Fallback

    Args:
        data_email:        Data do email para compor o prefixo da data.
        anexos_originais:  Lista de arquivos baixados do email (inclui XMLs).
        pdfs_convertidos:  PDFs já convertidos dos anexos (opcional; usado
                           como segunda fonte para extração de texto).

    Returns:
        Nome de arquivo final, ex.: "17-06-2026 - AUTO PECAS BRASIL LTDA.pdf"
    """
    data_str = data_email.strftime("%d-%m-%Y")
    nome_fornecedor: str | None = None

    # ── 1. Prioridade máxima: XML da NF-e ────────────────────────────────────
    xml_path = detectar_xml(anexos_originais)
    if xml_path:
        logger.info("XML detectado: tentando extrair fornecedor via NF-e...")
        nome_fornecedor = extrair_fornecedor_xml(xml_path)

    # ── 2. Segunda prioridade: PDF de Nota Fiscal ─────────────────────────────
    if not nome_fornecedor:
        fontes_pdf = list(pdfs_convertidos or []) + [
            a for a in anexos_originais if a.suffix.lower() == ".pdf"
        ]
        for pdf in fontes_pdf:
            if not pdf.exists():
                continue
            logger.info("Tentando extrair fornecedor (NF-e) de '%s'...", pdf.name)
            nome_fornecedor = extrair_fornecedor_nfe_pdf(pdf)
            if nome_fornecedor:
                break

    # ── 3. Terceira prioridade: Boleto ────────────────────────────────────────
    if not nome_fornecedor:
        fontes_pdf = list(pdfs_convertidos or []) + [
            a for a in anexos_originais if a.suffix.lower() == ".pdf"
        ]
        for pdf in fontes_pdf:
            if not pdf.exists():
                continue
            logger.info("Tentando extrair cedente (boleto) de '%s'...", pdf.name)
            nome_fornecedor = extrair_fornecedor_boleto(pdf)
            if nome_fornecedor:
                break

    # ── 4. Fallback ───────────────────────────────────────────────────────────
    if not nome_fornecedor:
        logger.warning(
            "Fornecedor não identificado em nenhum anexo; usando fallback '%s'.",
            FALLBACK_NOME,
        )
        nome_fornecedor = FALLBACK_NOME
    else:
        nome_fornecedor = sanitizar_nome_arquivo(nome_fornecedor)

    nome_final = f"{data_str} - {nome_fornecedor}.pdf"
    logger.info("Nome final gerado: %s", nome_final)
    return nome_final
