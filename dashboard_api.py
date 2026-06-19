"""
dashboard_api.py - Servidor leve para o Dashboard de Monitoramento em tempo real.

Inicie com:  python dashboard_api.py
Acesse em:  http://localhost:5000

Não requer dependências extras além do que já está no requirements.txt.
Usa apenas stdlib (http.server + sqlite3 + json).
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# ── Configuração ──────────────────────────────────────────────────────────────

PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")

# Localiza o banco de dados: usa DB_PATH do .env ou busca ao lado deste script
try:
    from config import DB_PATH
except ImportError:
    DB_PATH = Path(__file__).parent / "processamentos.db"

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


# ── Banco ─────────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def query_stats() -> dict:
    """Retorna estatísticas gerais dos processamentos."""
    with _connect() as conn:
        total     = conn.execute("SELECT COUNT(*) FROM processamentos").fetchone()[0]
        concluido = conn.execute("SELECT COUNT(*) FROM processamentos WHERE status='concluido'").fetchone()[0]
        erro      = conn.execute("SELECT COUNT(*) FROM processamentos WHERE status='erro'").fetchone()[0]
        processan = conn.execute("SELECT COUNT(*) FROM processamentos WHERE status='processando'").fetchone()[0]

        # Hoje
        hoje = datetime.utcnow().date().isoformat()
        hoje_total = conn.execute(
            "SELECT COUNT(*) FROM processamentos WHERE criado_em >= ?", (hoje,)
        ).fetchone()[0]

        # Tempo médio de processamento (segundos) para os concluídos
        avg_row = conn.execute("""
            SELECT AVG(
                (julianday(atualizado_em) - julianday(criado_em)) * 86400
            ) as avg_s
            FROM processamentos
            WHERE status = 'concluido'
        """).fetchone()
        avg_s = round(avg_row["avg_s"] or 0, 1)

        # Por classificação
        classif = conn.execute("""
            SELECT classificacao, COUNT(*) as n
            FROM processamentos
            WHERE status = 'concluido' AND classificacao IS NOT NULL
            GROUP BY classificacao
            ORDER BY n DESC
        """).fetchall()

        # Por remetente (top 5)
        remetentes = conn.execute("""
            SELECT remetente, COUNT(*) as n
            FROM processamentos
            GROUP BY remetente
            ORDER BY n DESC
            LIMIT 5
        """).fetchall()

        # Volume por dia (últimos 14 dias)
        volume_dia = conn.execute("""
            SELECT DATE(criado_em) as dia,
                   COUNT(*) as total,
                   SUM(CASE WHEN status='concluido' THEN 1 ELSE 0 END) as ok,
                   SUM(CASE WHEN status='erro' THEN 1 ELSE 0 END) as erros
            FROM processamentos
            WHERE criado_em >= DATE('now', '-14 days')
            GROUP BY dia
            ORDER BY dia
        """).fetchall()

    return {
        "total": total,
        "concluido": concluido,
        "erro": erro,
        "processando": processan,
        "hoje": hoje_total,
        "taxa_sucesso": round((concluido / total * 100) if total else 0, 1),
        "tempo_medio_s": avg_s,
        "por_classificacao": [dict(r) for r in classif],
        "por_remetente": [dict(r) for r in remetentes],
        "volume_por_dia": [dict(r) for r in volume_dia],
    }


def query_processamentos(status: str = None, limit: int = 100, offset: int = 0, busca: str = None) -> dict:
    """Retorna lista paginada de processamentos com filtros opcionais."""
    where_parts = []
    params = []

    if status and status != "todos":
        where_parts.append("status = ?")
        params.append(status)

    if busca:
        where_parts.append("(assunto LIKE ? OR remetente LIKE ? OR nome_pdf LIKE ? OR classificacao LIKE ?)")
        like = f"%{busca}%"
        params.extend([like, like, like, like])

    where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    with _connect() as conn:
        total_filtrado = conn.execute(
            f"SELECT COUNT(*) FROM processamentos {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""SELECT * FROM processamentos {where}
                ORDER BY criado_em DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

    return {
        "total": total_filtrado,
        "limit": limit,
        "offset": offset,
        "items": [dict(r) for r in rows],
    }


def query_erros(limit: int = 50) -> list:
    """Retorna apenas os registros com erro, com detalhes completos."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT * FROM processamentos
            WHERE status = 'erro'
            ORDER BY atualizado_em DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def query_rastreabilidade(gmail_message_id: str) -> dict | None:
    """Retorna rastreabilidade completa de um email específico."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM processamentos WHERE gmail_message_id = ?",
            (gmail_message_id,),
        ).fetchone()
    return dict(row) if row else None


# ── Servidor HTTP ─────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        """Suprime logs de acesso repetitivos (polling)."""
        if "/api/" not in args[0]:
            super().log_message(format, *args)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")
        qs     = parse_qs(parsed.query)

        # ── Dashboard HTML ────────────────────────────────────────────────────
        if path in ("", "/", "/dashboard"):
            self._serve_file(DASHBOARD_HTML, "text/html; charset=utf-8")
            return

        # ── API endpoints ─────────────────────────────────────────────────────
        if path == "/api/stats":
            self._json(query_stats())

        elif path == "/api/processamentos":
            status = qs.get("status", ["todos"])[0]
            limit  = int(qs.get("limit", [100])[0])
            offset = int(qs.get("offset", [0])[0])
            busca  = qs.get("busca", [None])[0]
            self._json(query_processamentos(status, limit, offset, busca))

        elif path == "/api/erros":
            self._json(query_erros())

        elif path.startswith("/api/rastrear/"):
            mid = path.split("/api/rastrear/", 1)[1]
            result = query_rastreabilidade(mid)
            if result:
                self._json(result)
            else:
                self._json({"erro": "Não encontrado"}, status=404)

        elif path == "/api/status":
            self._json({
                "ok": True,
                "db": str(DB_PATH),
                "db_existe": DB_PATH.exists(),
                "hora": datetime.utcnow().isoformat(),
            })

        else:
            self._json({"erro": "Endpoint não encontrado"}, status=404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, data: dict | list, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path, content_type: str):
        if not path.exists():
            self._json({"erro": f"Arquivo não encontrado: {path}"}, status=404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"⚠️  Banco não encontrado em: {DB_PATH}")
        print("   O dashboard funcionará, mas sem dados até a automação criar o banco.")

    server = HTTPServer((HOST, PORT), DashboardHandler)
    print(f"""
╔══════════════════════════════════════════════╗
║   Dashboard de Monitoramento — iniciado      ║
╠══════════════════════════════════════════════╣
║  URL:  http://localhost:{PORT:<20}║
║  DB:   {str(DB_PATH):<38}║
╚══════════════════════════════════════════════╝
  Ctrl+C para parar
""")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
        sys.exit(0)