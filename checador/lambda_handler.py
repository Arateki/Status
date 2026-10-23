"""Ponto de entrada (handler) da AWS Lambda checadora."""

import json
import logging
import os
from typing import Any

from .checker import checar_todos
from .config import carregar_sistemas
from .evaluator import avaliar_checagem
from .notifier import enviar_alerta_sns
from .storage import (
    carregar_estado_sistema,
    construir_status_json,
    publicar_status_json,
    salvar_estado_e_checagem,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Função executada a cada 1 minuto pelo EventBridge Scheduler."""
    sistemas = carregar_sistemas()
    logger.info("Iniciando checagem de %d sistemas...", len(sistemas))

    # 1. Carrega estados anteriores do DynamoDB
    estados_anteriores = {}
    for s in sistemas:
        sid = s["id"]
        estados_anteriores[sid] = carregar_estado_sistema(sid)

    # 2. Executa checagens HTTP em paralelo
    resultados = checar_todos(sistemas)

    # 3. Avalia resultados e transições
    novos_estados = {}
    alertas_disparados = []

    for res in resultados:
        est_ant = estados_anteriores.get(res.sistema_id)
        novo_est, acao, dados_alerta = avaliar_checagem(est_ant, res)
        novos_estados[res.sistema_id] = novo_est

        # Dispara alertas SNS quando houver transição de estado
        if acao in ("notificar_queda", "notificar_volta") and dados_alerta:
            sucesso_sns = enviar_alerta_sns(acao, dados_alerta)
            alertas_disparados.append({
                "sistema": res.sistema_id,
                "acao": acao,
                "enviado": sucesso_sns,
            })

        # Salva no DynamoDB
        salvar_estado_e_checagem(novo_est, res.to_dict())

    # 4. Constrói e publica status.json estático no S3/R2
    status_dados = construir_status_json(sistemas, novos_estados)
    publicar_status_json(status_dados)

    resumo = {
        "status_geral": status_dados["status_geral"],
        "sistemas_checados": len(resultados),
        "alertas": alertas_disparados,
    }
    logger.info("Checagem concluída com sucesso: %s", json.dumps(resumo))

    return {
        "statusCode": 200,
        "body": json.dumps(resumo),
    }
