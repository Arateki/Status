"""Ponto de entrada (handler) da AWS Lambda checadora."""

import json
import logging
from typing import Any, Optional

from .checker import checar_todos
from .config import carregar_sistemas
from .evaluator import EstadoSistema, avaliar_checagem
from .notifier import enviar_alerta_sns
from .storage import (
    ErroGravacaoDynamoDB,
    ErroLeituraDynamoDB,
    ErroPublicacaoR2,
    carregar_baldes_24h,
    carregar_estado_sistema,
    confirmar_alerta_enviado,
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

    # 1. Carrega estados anteriores do DynamoDB com salvaguarda
    estados_anteriores: dict[str, Optional[EstadoSistema]] = {}
    try:
        for s in sistemas:
            sid = s["id"]
            est = carregar_estado_sistema(sid)
            if est is not None:
                baldes = carregar_baldes_24h(sid)
                if baldes:
                    est.baldes_24h = baldes
            estados_anteriores[sid] = est
    except ErroLeituraDynamoDB as err:
        logger.error("Abortando execução para preservar histórico existente: %s", err)
        raise

    # 2. Executa checagens HTTP em paralelo
    resultados = checar_todos(sistemas)

    # 3. Avalia resultados e transições
    novos_estados: dict[str, EstadoSistema] = {}
    alertas_disparados = []

    for res in resultados:
        est_ant = estados_anteriores.get(res.sistema_id)
        versao_antiga = est_ant.versao if est_ant else 0
        novo_est, acao, dados_alerta = avaliar_checagem(est_ant, res)
        novos_estados[res.sistema_id] = novo_est

        # Salva no DynamoDB ANTES de disparar qualquer notificação SNS
        # Se a escrita falhar, lança ErroGravacaoDynamoDB e aborta sem disparar SNS indevido
        salvar_estado_e_checagem(
            estado=novo_est,
            resultado_checagem=res.to_dict(),
            versao_esperada=versao_antiga,
        )

        # Dispara alertas SNS somente após persistência garantida no DynamoDB
        alerta_para_enviar = novo_est.alerta_pendente
        if alerta_para_enviar:
            acao_envio = alerta_para_enviar.get("acao", acao)
            dados_envio = alerta_para_enviar.get("dados", dados_alerta)
            sucesso_sns = enviar_alerta_sns(acao_envio, dados_envio)
            alertas_disparados.append({
                "sistema": res.sistema_id,
                "acao": acao_envio,
                "enviado": sucesso_sns,
            })
            if sucesso_sns:
                confirmar_alerta_enviado(novo_est)
            else:
                logger.warning(
                    "Envio de alerta SNS falhou para %s; alerta mantido como pendente.",
                    res.sistema_id,
                )

    # 4. Constrói e publica status.json estático no S3/R2 (lança ErroPublicacaoR2 se falhar)
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
