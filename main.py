"""
main.py - Orquestrador principal do pipeline de automação documental.

Fluxo por email:
  1. Listar emails na label PROCESSAR
  2. Verificar duplicidade (SQLite)
  3. Extrair metadados, corpo e anexos (GmailService)
  4. Converter corpo HTML → PDF  (PdfConverter)
  5. Converter cada anexo → PDF  (PdfConverter)
  6. Mesclar todos os PDFs       (PdfMerger)
  7. Classificar documento       (Classifier)
  8. Gerar nome final            (AAAA-MM-DD_ASSUNTO.pdf)
  9. Criar pasta no Drive e fazer upload (DriveService)
 10. Registrar no SQLite + mover email para label PROCESSADO
 11. Limpar arquivos temporários (SOMENTE após upload confirmado)
"""

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


# ── Pipeline ──────────────────────────────────────────────────────────────────

class Pipeline:
    """Orquestra o processamento de emails."""

    def __init__(self) -> None:
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
            logger.info("✓ Email %s processado com sucesso → %s", message_id, nome_pdf)

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

            #ALTERACAO PARA PUXAR O ASSUNTO 
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

            # 5b. Classifica cada PDF de anexo individualmente para que o
            #     merger possa ordenar os documentos por categoria.
            classificacoes_anexos = self._classificar_anexos(anexos_pdf)

            # 6. Mesclagem — nome provisório para o arquivo de trabalho
            destino_final = pasta_trabalho / "_merged_temp.pdf"
            self.merger.mesclar(
                email_pdf,
                anexos_pdf,
                destino_final,
                classificacoes_anexos=classificacoes_anexos,
            )

            # 6b. Geração do nome final com identificação do fornecedor
            #     Hierarquia: XML NF-e → PDF DANFE → PDF Boleto → fallback
            nome_final = gerar_nome_final(
                data_email=data_email,
                anexos_originais=email.anexos,
                pdfs_convertidos=anexos_pdf,
            )
            destino_renomeado = pasta_trabalho / nome_final
            destino_final.rename(destino_renomeado)
            destino_final = destino_renomeado
            logger.info("Arquivo renomeado para: %s", nome_final)

            # 7. Classificação
            texto_para_classificar = email.assunto + " " + email.corpo_texto
            classificacao = self.classifier.classificar(texto_para_classificar)
            logger.info("Classificação: %s", classificacao)

            # 9. Upload Drive
            pasta_drive_id = self.drive.garantir_estrutura(data_email)
            drive_link = self.drive.fazer_upload(destino_final, pasta_drive_id)

        finally:
            # CORREÇÃO: a limpeza agora ocorre SEMPRE ao final do bloco try,
            # garantindo que arquivos temporários sejam removidos mesmo em caso
            # de erro. O registro de erro é feito pelo chamador (_processar_email).
            # Se o upload falhou, a exceção já foi propagada antes desta linha.
            shutil.rmtree(pasta_trabalho, ignore_errors=True)
            logger.debug("Pasta de trabalho removida: %s", pasta_trabalho)

        return nome_final, drive_link, classificacao

    def _classificar_anexos(self, pdfs: list[Path]) -> list[str]:
        """
        Classifica cada PDF de anexo extraindo seu texto e aplicando o
        Classifier — retorna uma lista na mesma ordem de `pdfs`.
        """
        from supplier_extractor import _extrair_texto_pdf  # importação local para evitar ciclo

        classificacoes: list[str] = []
        for pdf in pdfs:
            try:
                texto = _extrair_texto_pdf(pdf)
                classificacao = self.classifier.classificar(texto) if texto else "Outros"
            except Exception as exc:
                logger.warning("Erro ao classificar '%s': %s — usando 'Outros'.", pdf.name, exc)
                classificacao = "Outros"
            logger.debug("Anexo '%s' classificado como '%s'.", pdf.name, classificacao)
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
                logger.info("Anexo %d/%d convertido: %s", i, len(anexos), pdf.name)
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
# NOTA: o dashboard NÃO sobe mais aqui. Como este script é reiniciado a cada
# ciclo pelo iniciar_automacao.bat (loop com `py main.py`), uma thread daemon
# criada aqui morre junto com o processo a cada execução — o dashboard ficaria
# "no ar" só durante os poucos segundos do pipeline. Agora ele roda como
# processo independente e persistente, iniciado por iniciar_dashboard.bat.

configurar_logging()
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║   Automação Documental — Gmail + Drive   ║")
    logger.info("╚══════════════════════════════════════════╝")

    Pipeline().executar()
