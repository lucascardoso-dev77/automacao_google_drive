"""
main.py - Orquestrador principal do pipeline de automação documental.

Fluxo por email:
  1. [NOVO] Verificar/selecionar pasta raiz no Drive (folder_selector)
  2. Listar emails na label PROCESSAR
  3. Verificar duplicidade (SQLite)
  4. Extrair metadados, corpo e anexos (GmailService)
  5. Converter corpo HTML → PDF  (PdfConverter)
  6. Converter cada anexo → PDF  (PdfConverter)
  7. Mesclar todos os PDFs       (PdfMerger)
  8. Classificar documento       (Classifier)
  9. Gerar nome final            (AAAA-MM-DD_ASSUNTO.pdf)
 10. Criar pasta AAAA/MM no Drive e fazer upload (DriveService)
 11. Registrar no SQLite + mover email para label PROCESSADO
 12. Limpar arquivos temporários (SOMENTE após upload confirmado)

Argumentos de linha de comando:
  --reconfigura   Abre o seletor de pasta mesmo que já haja configuração salva.
                  Útil para trocar a pasta de destino sem apagar drive_config.json.
"""

import argparse
import logging
import shutil
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from config import LOG_FILE, LOG_LEVEL, OUTPUT_DIR
from classifier import Classifier
from database import Database
from drive_service import DriveService
from folder_selector import configurar_pasta_raiz
from gmail_service import GmailService, EmailData
from pdf_converter import PdfConverter
from pdf_merger import PdfMerger
from supplier_extractor import gerar_nome_final

# ── Logging ───────────────────────────────────────────────────────────────────


def configurar_logging() -> None:
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
    logging.basicConfig(level=LOG_LEVEL, format=fmt, handlers=handlers)


# ── Argumentos ────────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automação Documental — Gmail + Google Drive"
    )
    parser.add_argument(
        "--reconfigura",
        action="store_true",
        help=(
            "Abre o seletor de pasta do Drive mesmo que já exista "
            "uma configuração salva em drive_config.json."
        ),
    )
    return parser.parse_args()


# ── Pipeline ──────────────────────────────────────────────────────────────────


class Pipeline:
    """Orquestra o processamento de emails."""

    def __init__(self, pasta_raiz_id: str) -> None:
        """
        Args:
            pasta_raiz_id: ID da pasta raiz no Drive escolhida pelo usuário.
                           Passado a garantir_estrutura em cada upload.
        """
        self.pasta_raiz_id = pasta_raiz_id
        self.gmail = GmailService()
        self.drive = DriveService()
        self.converter = PdfConverter()   # Injete ocr_hook= para ativar OCR
        self.merger = PdfMerger()
        self.classifier = Classifier()
        self.db = Database()

    # ── Ponto de entrada ──────────────────────────────────────────────────────

    def executar(self) -> None:
        ids = self.gmail.listar_mensagens_nao_processadas()
        if not ids:
            logger.info("Nenhum email para processar.")
            return

        logger.info("%d email(s) a processar.", len(ids))
        for message_id in ids:
            self._processar_email(message_id)

    # ── Processamento individual ──────────────────────────────────────────────

    def _processar_email(self, message_id: str) -> None:
        # Verificação de duplicidade
        if self.db.ja_processado(message_id):
            logger.info("Email %s já processado — ignorando.", message_id)
            return

        logger.info("═" * 60)
        logger.info("Iniciando processamento do email: %s", message_id)

        email = self.gmail.obter_email(message_id)
        if not email:
            logger.error("Não foi possível obter o email %s.", message_id)
            return

        data_email = self._parse_data(email.data)
        self.db.registrar_inicio(
            gmail_message_id=message_id,
            assunto=email.assunto,
            remetente=email.remetente,
            data_email=email.data,
        )

        try:
            nome_pdf, drive_link, classificacao = self._pipeline(email, data_email)
            self.db.registrar_conclusao(
                gmail_message_id=message_id,
                nome_pdf=nome_pdf,
                drive_link=drive_link,
                classificacao=classificacao,
            )
            self.gmail.mover_para_processado(message_id)
            logger.info(
                "✓ Email %s processado com sucesso → %s", message_id, nome_pdf
            )

        except Exception as exc:
            logger.exception("✗ Erro ao processar email %s: %s", message_id, exc)
            self.db.registrar_erro(message_id, str(exc))

    def _pipeline(
        self, email: EmailData, data_email: datetime
    ) -> tuple[str, str, str]:
        """Executa as etapas de conversão, mesclagem e upload."""
        pasta_trabalho = OUTPUT_DIR / email.message_id
        pasta_trabalho.mkdir(parents=True, exist_ok=True)

        try:
            # 4. Corpo HTML → PDF
            html_content = email.corpo_html or f"<pre>{email.corpo_texto}</pre>"
            email_pdf = pasta_trabalho / "00_email.pdf"
            self.converter.html_string_para_pdf(
                html=html_content,
                destino=email_pdf,
                assunto=email.assunto,
                remetente=email.remetente,
                data_email=email.data,
                nomes_anexos=[a.name for a in email.anexos],
                message_id=email.message_id,
            )

            # 5. Anexos → PDF
            anexos_pdf = self._converter_anexos(email.anexos, pasta_trabalho)

            # 5b. Classifica cada PDF de anexo individualmente
            classificacoes_anexos = self._classificar_anexos(anexos_pdf)

            # 6. Mesclagem
            destino_final = pasta_trabalho / "_merged_temp.pdf"
            self.merger.mesclar(
                email_pdf,
                anexos_pdf,
                destino_final,
                classificacoes_anexos=classificacoes_anexos,
            )

            # 6b. Geração do nome final
            nome_final = gerar_nome_final(
                data_email=data_email,
                anexos_originais=email.anexos,
                pdfs_convertidos=anexos_pdf,
            )
            destino_renomeado = pasta_trabalho / nome_final
            destino_final.rename(destino_renomeado)
            destino_final = destino_renomeado
            logger.info("Arquivo renomeado para: %s", nome_final)

            # 7. Classificação do email
            texto_para_classificar = email.assunto + " " + email.corpo_texto
            classificacao = self.classifier.classificar(texto_para_classificar)
            logger.info("Classificação: %s", classificacao)

            # 9. Upload Drive — usa a pasta raiz configurada pelo usuário
            pasta_drive_id = self.drive.garantir_estrutura(
                data_email, raiz_id=self.pasta_raiz_id
            )
            drive_link = self.drive.fazer_upload(destino_final, pasta_drive_id)

        finally:
            # Limpeza sempre ao final, mesmo em caso de erro
            shutil.rmtree(pasta_trabalho, ignore_errors=True)
            logger.debug("Pasta de trabalho removida: %s", pasta_trabalho)

        return nome_final, drive_link, classificacao

    def _classificar_anexos(self, pdfs: list[Path]) -> list[str]:
        """Classifica cada PDF de anexo e retorna lista na mesma ordem."""
        from supplier_extractor import _extrair_texto_pdf

        classificacoes: list[str] = []
        for pdf in pdfs:
            try:
                texto = _extrair_texto_pdf(pdf)
                classificacao = self.classifier.classificar(texto) if texto else "Outros"
            except Exception as exc:
                logger.warning(
                    "Erro ao classificar '%s': %s — usando 'Outros'.", pdf.name, exc
                )
                classificacao = "Outros"
            logger.debug(
                "Anexo '%s' classificado como '%s'.", pdf.name, classificacao
            )
            classificacoes.append(classificacao)
        return classificacoes

    def _converter_anexos(
        self, anexos: list[Path], pasta_saida: Path
    ) -> list[Path]:
        """Converte cada anexo para PDF mantendo a ordem original."""
        pdfs: list[Path] = []
        for i, anexo in enumerate(anexos, start=1):
            try:
                pdf = self.converter.converter(
                    anexo,
                    destino_dir=pasta_saida / f"anexo_{i:02d}",
                )
                pdfs.append(pdf)
                logger.info(
                    "Anexo %d/%d convertido: %s", i, len(anexos), pdf.name
                )
            except Exception as exc:
                logger.error(
                    "Erro ao converter anexo '%s': %s — ignorando.", anexo.name, exc
                )
        return pdfs

    # ── Utilitários ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_data(data_str: str) -> datetime:
        try:
            return parsedate_to_datetime(data_str)
        except Exception:
            return datetime.utcnow()


# ── Execução ──────────────────────────────────────────────────────────────────

configurar_logging()
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    args = _parse_args()

    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║   Automação Documental — Gmail + Drive   ║")
    logger.info("╚══════════════════════════════════════════╝")

    # ── 1. Autenticar Drive (necessário antes do seletor de pastas) ───────────
    drive_temp = DriveService()

    # ── 2. Configurar (ou confirmar) a pasta raiz no Drive ────────────────────
    #   • Primeira execução ou --reconfigura → abre seletor interativo
    #   • Execuções seguintes → lê drive_config.json e exibe confirmação
    try:
        pasta_config = configurar_pasta_raiz(
            service=drive_temp.service,
            forcar=args.reconfigura,
        )
    except KeyboardInterrupt as exc:
        logger.warning("Configuração de pasta cancelada: %s", exc)
        sys.exit(0)

    pasta_raiz_id: str = pasta_config["folder_id"]
    logger.info(
        "Pasta de destino: %s (id=%s)",
        pasta_config["caminho"],
        pasta_raiz_id,
    )

    # ── 3. Executar pipeline de processamento de emails ───────────────────────
    Pipeline(pasta_raiz_id=pasta_raiz_id).executar()