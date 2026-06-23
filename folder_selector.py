"""
folder_selector.py - Seleção interativa da pasta raiz no Google Drive.

Executado automaticamente na primeira inicialização da automação.
Também pode ser forçado via argumento --reconfigura no main.py.

Comportamento:
  • Navega pela árvore de pastas do Drive via teclado
  • Permite criar novas subpastas sem sair do seletor
  • Salva o ID e o caminho da pasta escolhida em drive_config.json
  • Nas próximas execuções, usa o arquivo salvo sem perguntar novamente

Estrutura criada no Drive:
  <PASTA SELECIONADA> / AAAA / MM / arquivo.pdf
"""

import json
import logging
from pathlib import Path
from typing import Any

from googleapiclient.errors import HttpError

from config import BASE_DIR

logger = logging.getLogger(__name__)

# Arquivo onde a configuração de pasta é persistida
DRIVE_CONFIG_FILE = BASE_DIR / "drive_config.json"

MIME_FOLDER = "application/vnd.google-apps.folder"

# ── Persistência ──────────────────────────────────────────────────────────────


def carregar_pasta_configurada() -> dict | None:
    """
    Retorna o dicionário salvo em drive_config.json ou None se não existir.
    Formato: {"folder_id": str, "folder_name": str, "caminho": str}
    """
    if not DRIVE_CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(DRIVE_CONFIG_FILE.read_text(encoding="utf-8"))
        # Valida campos obrigatórios
        if all(k in data for k in ("folder_id", "folder_name", "caminho")):
            return data
    except Exception as exc:
        logger.warning("drive_config.json inválido (%s) — será reconfigurado.", exc)
    return None


def salvar_pasta_configurada(folder_id: str, folder_name: str, caminho: str) -> None:
    """Persiste a pasta selecionada em drive_config.json."""
    data = {
        "folder_id": folder_id,
        "folder_name": folder_name,
        "caminho": caminho,
    }
    DRIVE_CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("Pasta raiz salva: '%s' (id=%s)", caminho, folder_id)


# ── Navegação no Drive ────────────────────────────────────────────────────────


def _listar_subpastas(service: Any, pai_id: str | None) -> list[tuple[str, str]]:
    """
    Retorna lista de (folder_id, folder_name) dentro de pai_id.
    Se pai_id=None, lista a raiz do Drive ('Meu Drive').
    """
    filtro_pai = f"'{pai_id}' in parents" if pai_id else "'root' in parents"
    q = f"{filtro_pai} and mimeType='{MIME_FOLDER}' and trashed=false"
    try:
        resultado = (
            service.files()
            .list(
                q=q,
                spaces="drive",
                fields="files(id, name)",
                orderBy="name",
                pageSize=100,
            )
            .execute()
        )
        return [(f["id"], f["name"]) for f in resultado.get("files", [])]
    except HttpError as exc:
        logger.error("Erro ao listar pastas: %s", exc)
        return []


def _criar_subpasta(service: Any, nome: str, pai_id: str | None) -> str:
    """Cria uma nova pasta no Drive e retorna seu ID."""
    meta: dict[str, Any] = {"name": nome, "mimeType": MIME_FOLDER}
    if pai_id:
        meta["parents"] = [pai_id]
    pasta = service.files().create(body=meta, fields="id").execute()
    return pasta["id"]


# ── Interface interativa de terminal ──────────────────────────────────────────


def _exibir_cabecalho(caminho_str: str) -> None:
    print("\n" + "═" * 62)
    print(f"  📁  {caminho_str}")
    print("═" * 62)


def selecionar_pasta_interativo(service: Any) -> tuple[str, str, str]:
    """
    Navega interativamente pelas pastas do Drive no terminal.

    Retorna:
        (folder_id, folder_name, caminho_completo)

    O caminho_completo é uma string legível como:
        "Meu Drive / Empresa / Documentos Fiscais"
    """
    # Pilha de navegação: cada item é (folder_id | None, folder_name)
    # None representa a raiz do Drive
    historico: list[tuple[str | None, str]] = [(None, "Meu Drive")]

    while True:
        pai_id, pai_nome = historico[-1]
        caminho_str = " / ".join(nome for _, nome in historico)

        pastas = _listar_subpastas(service, pai_id)

        _exibir_cabecalho(caminho_str)

        if not pastas:
            print("  (nenhuma subpasta encontrada aqui)")
        else:
            for i, (_, fname) in enumerate(pastas, start=1):
                print(f"  [{i:>2}]  📁  {fname}")

        print()
        opcoes = ["  [S]  ✅  Selecionar ESTA pasta como destino"]
        if len(historico) > 1:
            opcoes.append("  [V]  ⬆️   Voltar para a pasta anterior")
        opcoes += [
            "  [N]  ➕  Criar nova subpasta aqui",
            "  [Q]  ❌  Cancelar",
        ]
        print("\n".join(opcoes))
        print()

        escolha = input("  Opção: ").strip().upper()

        # ── Selecionar pasta atual ─────────────────────────────────────────
        if escolha == "S":
            folder_id = pai_id or "root"
            return folder_id, pai_nome, caminho_str

        # ── Voltar ────────────────────────────────────────────────────────
        elif escolha == "V" and len(historico) > 1:
            historico.pop()

        # ── Criar nova subpasta ────────────────────────────────────────────
        elif escolha == "N":
            nome = input("  Nome da nova pasta: ").strip()
            if not nome:
                print("  ⚠️  Nome não pode ser vazio.")
                continue
            try:
                novo_id = _criar_subpasta(service, nome, pai_id)
                print(f"  ✅  Pasta '{nome}' criada com sucesso.")
                historico.append((novo_id, nome))
            except HttpError as exc:
                print(f"  ❌  Erro ao criar pasta: {exc}")

        # ── Cancelar ──────────────────────────────────────────────────────
        elif escolha == "Q":
            raise KeyboardInterrupt("Configuração de pasta cancelada pelo usuário.")

        # ── Navegar para subpasta numerada ─────────────────────────────────
        elif escolha.isdigit():
            idx = int(escolha) - 1
            if 0 <= idx < len(pastas):
                fid, fname = pastas[idx]
                historico.append((fid, fname))
            else:
                print(f"  ⚠️  Número inválido (escolha entre 1 e {len(pastas)}).")

        else:
            print("  ⚠️  Opção inválida.")


# ── Ponto de entrada principal ────────────────────────────────────────────────


def configurar_pasta_raiz(service: Any, forcar: bool = False) -> dict:
    """
    Verifica se há uma pasta já configurada e, se não houver (ou forcar=True),
    abre o seletor interativo e persiste o resultado.

    Args:
        service:  Objeto autenticado da API do Google Drive (drive_service._service).
        forcar:   Se True, ignora o arquivo salvo e abre o seletor mesmo assim.

    Returns:
        dict com chaves: folder_id, folder_name, caminho.
    """
    config = carregar_pasta_configurada()

    if config and not forcar:
        print()
        print("╔" + "═" * 60 + "╗")
        print("║   📁  Pasta de destino configurada:                        ║")
        print(f"║       {config['caminho']:<54}║")
        print("╠" + "═" * 60 + "╣")
        print("║   Use  --reconfigura  para escolher outra pasta.           ║")
        print("╚" + "═" * 60 + "╝")
        print()
        return config

    # Exibe banner do seletor
    print()
    print("╔" + "═" * 60 + "╗")
    print("║   CONFIGURAÇÃO DA PASTA DE DESTINO NO GOOGLE DRIVE         ║")
    print("╠" + "═" * 60 + "╣")
    print("║   Navegue pelas pastas e pressione [S] para selecionar.    ║")
    print("║   Os arquivos serão salvos em:                              ║")
    print("║      <PASTA ESCOLHIDA> / AAAA / MM / arquivo.pdf           ║")
    print("╚" + "═" * 60 + "╝")

    folder_id, folder_name, caminho = selecionar_pasta_interativo(service)
    salvar_pasta_configurada(folder_id, folder_name, caminho)

    print()
    print(f"  ✅  Configuração salva! Pasta raiz: {caminho}")
    print()

    return {"folder_id": folder_id, "folder_name": folder_name, "caminho": caminho}