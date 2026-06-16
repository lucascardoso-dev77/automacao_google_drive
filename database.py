"""
database.py - Gerenciamento do banco SQLite para registro de processamentos.
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from config import DB_PATH

logger = logging.getLogger(__name__)


class Database:
    """Gerencia operações no banco SQLite."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._init_db()

    # ── Inicialização ─────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Cria a tabela e índices se ainda não existirem."""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processamentos (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    gmail_message_id  TEXT    NOT NULL UNIQUE,
                    assunto           TEXT,
                    remetente         TEXT,
                    data_email        TEXT,
                    nome_pdf          TEXT,
                    drive_link        TEXT,
                    classificacao     TEXT,
                    status            TEXT    NOT NULL DEFAULT 'pendente',
                    erro              TEXT,
                    criado_em         TEXT    NOT NULL,
                    atualizado_em     TEXT    NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_gmail_id
                ON processamentos (gmail_message_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_status
                ON processamentos (status)
            """)
        logger.info("Banco de dados inicializado em %s", self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Habilita WAL para melhor concorrência de leitura/escrita
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ── Consultas ─────────────────────────────────────────────────────────────

    def ja_processado(self, gmail_message_id: str) -> bool:
        """Retorna True se o email já foi processado com sucesso."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM processamentos WHERE gmail_message_id = ?",
                (gmail_message_id,),
            ).fetchone()
        return row is not None and row["status"] == "concluido"

    def registrar_inicio(
        self,
        gmail_message_id: str,
        assunto: str,
        remetente: str,
        data_email: str,
    ) -> int:
        """Insere um registro de início de processamento e retorna o ID."""
        agora = datetime.utcnow().isoformat()
        with self._connect() as conn:
            # CORREÇÃO: ON CONFLICT DO UPDATE não atualiza lastrowid de forma confiável.
            # Fazemos upsert em duas etapas para garantir o ID correto.
            conn.execute(
                """
                INSERT INTO processamentos
                    (gmail_message_id, assunto, remetente, data_email,
                     status, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, 'processando', ?, ?)
                ON CONFLICT(gmail_message_id) DO UPDATE SET
                    status        = 'processando',
                    atualizado_em = excluded.atualizado_em
                """,
                (gmail_message_id, assunto, remetente, data_email, agora, agora),
            )
            row = conn.execute(
                "SELECT id FROM processamentos WHERE gmail_message_id = ?",
                (gmail_message_id,),
            ).fetchone()
            return int(row["id"])

    def registrar_conclusao(
        self,
        gmail_message_id: str,
        nome_pdf: str,
        drive_link: str,
        classificacao: str,
    ) -> None:
        """Atualiza o registro com os dados do PDF gerado."""
        agora = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE processamentos
                SET nome_pdf      = ?,
                    drive_link    = ?,
                    classificacao = ?,
                    status        = 'concluido',
                    erro          = NULL,
                    atualizado_em = ?
                WHERE gmail_message_id = ?
                """,
                (nome_pdf, drive_link, classificacao, agora, gmail_message_id),
            )

    def registrar_erro(self, gmail_message_id: str, erro: str) -> None:
        """Marca o registro como falho e armazena a mensagem de erro."""
        agora = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE processamentos
                SET status        = 'erro',
                    erro          = ?,
                    atualizado_em = ?
                WHERE gmail_message_id = ?
                """,
                (erro, agora, gmail_message_id),
            )

    def listar_processamentos(self, limite: int = 100) -> list[sqlite3.Row]:
        """Retorna os últimos registros processados."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM processamentos ORDER BY criado_em DESC LIMIT ?",
                (limite,),
            ).fetchall()

    def listar_erros(self, limite: int = 50) -> list[sqlite3.Row]:
        """Retorna os registros com status 'erro' para reprocessamento."""
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM processamentos
                WHERE status = 'erro'
                ORDER BY atualizado_em DESC
                LIMIT ?
                """,
                (limite,),
            ).fetchall()

    def resetar_para_reprocessar(self, gmail_message_id: str) -> None:
        """
        Permite forçar o reprocessamento de um email que falhou,
        removendo o registro do banco.
        """
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM processamentos WHERE gmail_message_id = ?",
                (gmail_message_id,),
            )
        logger.info("Registro %s removido para reprocessamento.", gmail_message_id)
