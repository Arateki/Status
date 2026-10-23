"""Envio de notificações de alerta e recuperação via Amazon SNS."""

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)


def enviar_alerta_sns(
    acao: str,
    dados: dict[str, Any],
    topic_arn: Optional[str] = None,
    sns_client: Optional[Any] = None,
) -> bool:
    """Publica mensagem de alerta de queda ou volta no tópico SNS."""
    arn = topic_arn or os.environ.get("TOPIC_ARN")
    if not arn:
        logger.info("TOPIC_ARN não configurado; notificação SNS ignorada.")
        return False

    if sns_client is None:
        try:
            import boto3

            cfg = None
            try:
                from botocore.config import Config
                cfg = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2})
            except ImportError:
                pass

            kwargs: dict[str, Any] = {"config": cfg} if cfg else {}
            sns_client = boto3.client("sns", **kwargs)
        except Exception as err:
            logger.error("Erro ao inicializar cliente SNS: %s", err)
            return False

    nome = dados.get("nome", dados.get("sistema_id", "Sistema"))
    url = dados.get("url", "")

    if acao == "notificar_queda":
        assunto = f"[ALERTA] {nome} está FORA DO AR"
        motivo = dados.get("motivo", "Falha de resposta")
        codigo = dados.get("codigo_http")
        inicio = dados.get("inicio", "")

        codigo_linha = f"\nCódigo HTTP: {codigo}" if codigo else ""
        mensagem = (
            f"Alerta de Queda — Status da Arateki\n\n"
            f"Sistema: {nome}\n"
            f"URL: {url}\n"
            f"Status: FORA DO AR (2 falhas consecutivas)\n"
            f"Início: {inicio}\n"
            f"Motivo: {motivo}{codigo_linha}\n\n"
            f"Painel público: https://status.arateki.com\n"
        )
    elif acao == "notificar_volta":
        assunto = f"[RECUPERADO] {nome} voltou ao ar"
        inicio = dados.get("inicio", "")
        fim = dados.get("fim", "")
        duracao = dados.get("duracao_texto", "")

        mensagem = (
            f"Alerta de Recuperação — Status da Arateki\n\n"
            f"Sistema: {nome}\n"
            f"URL: {url}\n"
            f"Status: OPERACIONAL (recuperado com 1 checagem ok)\n"
            f"Início da queda: {inicio}\n"
            f"Volta confirmada: {fim}\n"
            f"Duração total fora do ar: {duracao}\n\n"
            f"Painel público: https://status.arateki.com\n"
        )
    else:
        return False

    try:
        # Assunto no SNS tem limite de 100 caracteres ASCII
        assunto_truncado = (assunto[:97] + "...") if len(assunto) > 100 else assunto
        sns_client.publish(
            TopicArn=arn,
            Subject=assunto_truncado,
            Message=mensagem,
        )
        logger.info("Notificação SNS enviada com sucesso: %s", assunto_truncado)
        return True
    except Exception as err:
        logger.error("Falha ao publicar no tópico SNS %s: %s", arn, err)
        return False
