"""Configurações do checador de status da Arateki."""

import json
import os

USER_AGENT = "AratekiStatus/1.0 (+https://status.arateki.com)"
TIMEOUT_PADRAO = 10  # segundos por sistema

SISTEMAS_PADRAO = [
    {
        "id": "arateki-site",
        "nome": "Site da Arateki",
        "url": "https://arateki.com",
        "espera_codigo": 200,
        "espera_corpo": None,
    },
    {
        "id": "raiznet-site",
        "nome": "Site da Raiznet",
        "url": "https://raiznet.com",
        "espera_codigo": 200,
        "espera_corpo": None,
    },
    {
        "id": "keyflow-api",
        "nome": "API do KeyFlow",
        "url": "https://keyflow-api.arateki.com/health",
        "espera_codigo": 200,
        "espera_corpo": "ok",
    },
]


def carregar_sistemas() -> list[dict]:
    """Carrega a lista de sistemas a monitorar.

    Prioridade:
    1. Arquivo sistemas.json local (se existir)
    2. Variável de ambiente SISTEMAS_JSON (se definida)
    3. Lista padrão do código
    """
    caminho_arquivo = os.path.join(os.path.dirname(__file__), "sistemas.json")
    if os.path.exists(caminho_arquivo):
        try:
            with open(caminho_arquivo, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    env_json = os.environ.get("SISTEMAS_JSON")
    if env_json:
        try:
            return json.loads(env_json)
        except Exception:
            pass

    return list(SISTEMAS_PADRAO)
