"""Módulo de persistência no DynamoDB e publicação do status.json no S3/R2."""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .evaluator import EstadoSistema, calcular_porcentagens

logger = logging.getLogger(__name__)

TTL_90_DIAS_SEGUNDOS = 90 * 86400


class ErroLeituraDynamoDB(Exception):
    """Lançada quando a leitura do DynamoDB falha."""
    pass


def obter_cliente_dynamodb():
    """Retorna cliente DynamoDB do boto3."""
    import boto3

    return boto3.client("dynamodb")


def obter_cliente_s3(
    endpoint_url: Optional[str] = None,
    access_key_id: Optional[str] = None,
    secret_access_key: Optional[str] = None,
):
    """Retorna cliente S3 do boto3 configurado para o Cloudflare R2."""
    import boto3

    ep = endpoint_url or os.environ.get("R2_ENDPOINT_URL") or os.environ.get("S3_ENDPOINT_URL")
    key = access_key_id or os.environ.get("R2_ACCESS_KEY_ID")
    secret = secret_access_key or os.environ.get("R2_SECRET_ACCESS_KEY")

    kwargs: dict[str, Any] = {}
    if ep:
        kwargs["endpoint_url"] = ep
    if key and secret:
        kwargs["aws_access_key_id"] = key
        kwargs["aws_secret_access_key"] = secret

    return boto3.client("s3", **kwargs)


def carregar_estado_sistema(
    sistema_id: str,
    table_name: Optional[str] = None,
    dynamo_client: Optional[Any] = None,
) -> Optional[EstadoSistema]:
    """Carrega o estado atual de um sistema a partir do DynamoDB.

    Retorna:
    - EstadoSistema se o item existe.
    - None se o item não existe no banco (sistema novo/primeira checagem).

    Lança:
    - ErroLeituraDynamoDB se houver falha de rede ou de serviço.
    """
    tabela = table_name or os.environ.get("TABLE_NAME", "arateki-status")
    if dynamo_client is None:
        try:
            dynamo_client = obter_cliente_dynamodb()
        except Exception as err:
            raise ErroLeituraDynamoDB(f"Não foi possível inicializar cliente DynamoDB: {err}") from err

    try:
        resp = dynamo_client.get_item(
            TableName=tabela,
            Key={
                "PK": {"S": sistema_id},
                "SK": {"S": "STATE"},
            },
            ConsistentRead=True,
        )
    except Exception as err:
        logger.error("Erro ao ler estado do sistema %s no DynamoDB: %s", sistema_id, err)
        raise ErroLeituraDynamoDB(f"Falha ao consultar estado de {sistema_id} no DynamoDB: {err}") from err

    item = resp.get("Item")
    if not item or "dados_json" not in item:
        return None

    try:
        dados = json.loads(item["dados_json"]["S"])
        return EstadoSistema.from_dict(dados)
    except Exception as err:
        logger.error("Erro ao decodificar dados_json do sistema %s: %s", sistema_id, err)
        raise ErroLeituraDynamoDB(f"JSON corrompido no estado de {sistema_id}: {err}") from err


def salvar_estado_e_checagem(
    estado: EstadoSistema,
    resultado_checagem: dict[str, Any],
    table_name: Optional[str] = None,
    dynamo_client: Optional[Any] = None,
) -> bool:
    """Salva o estado consolidado e o registro individual da checagem com TTL de 90 dias."""
    tabela = table_name or os.environ.get("TABLE_NAME", "arateki-status")
    if dynamo_client is None:
        try:
            dynamo_client = obter_cliente_dynamodb()
        except Exception as err:
            logger.warning("Não foi possível conectar ao DynamoDB para salvar: %s", err)
            return False

    agora_epoch = int(time.time())
    ttl_epoch = agora_epoch + TTL_90_DIAS_SEGUNDOS
    timestamp_iso = resultado_checagem.get("timestamp", datetime.now(timezone.utc).isoformat())

    try:
        # 1. Salva o estado atual consolidado
        estado_json = json.dumps(estado.to_dict())
        dynamo_client.put_item(
            TableName=tabela,
            Item={
                "PK": {"S": estado.sistema_id},
                "SK": {"S": "STATE"},
                "status": {"S": estado.status},
                "falhas_consecutivas": {"N": str(estado.falhas_consecutivas)},
                "ultima_checagem": {"S": estado.ultima_checagem or timestamp_iso},
                "dados_json": {"S": estado_json},
            },
        )

        # 2. Salva o registro bruto da checagem com TTL de 90 dias
        checagem_json = json.dumps(resultado_checagem)
        dynamo_client.put_item(
            TableName=tabela,
            Item={
                "PK": {"S": estado.sistema_id},
                "SK": {"S": f"CHECK#{timestamp_iso}"},
                "ok": {"BOOL": bool(resultado_checagem.get("ok", False))},
                "codigo_http": {"N": str(resultado_checagem.get("codigo_http") or 0)},
                "latencia_ms": {"N": str(resultado_checagem.get("latencia_ms") or 0)},
                "ttl": {"N": str(ttl_epoch)},
                "dados_json": {"S": checagem_json},
            },
        )
        return True
    except Exception as err:
        logger.error("Erro ao salvar dados no DynamoDB para %s: %s", estado.sistema_id, err)
        return False


def construir_status_json(
    sistemas_config: list[dict[str, Any]],
    estados: dict[str, EstadoSistema],
    agora_iso: Optional[str] = None,
) -> dict[str, Any]:
    """Constrói o documento status.json público e sanitizado."""
    timestamp = agora_iso or datetime.now(timezone.utc).isoformat()

    todos_no_ar = True
    lista_sistemas = []

    for cfg in sistemas_config:
        sid = cfg["id"]
        estado = estados.get(sid) or EstadoSistema(sistema_id=sid)

        if estado.status != "no_ar":
            todos_no_ar = False

        pcts = calcular_porcentagens(
            estado.historico_dias,
            estado.baldes_24h,
            timestamp_referencia=timestamp,
        )

        # Prepara baldes de 5 min formatados para o gráfico
        baldes_formatados = [
            {
                "inicio": b.get("inicio", ""),
                "latencia_ms": b.get("latencia_ms", 0),
                "ok": b.get("ok", True),
            }
            for b in estado.baldes_24h
        ]

        lista_sistemas.append({
            "id": sid,
            "nome": cfg.get("nome", sid),
            "url": cfg["url"],
            "status": estado.status,
            "ultima_checagem": estado.ultima_checagem or timestamp,
            "latencia_ms": estado.ultimo_tempo_ms,
            "codigo_http": estado.ultimo_codigo_http,
            "disponibilidade": pcts,
            "baldes_24h": baldes_formatados,
            "quedas_recentes": estado.quedas_recentes,
        })

    return {
        "gerado_em": timestamp,
        "status_geral": "operacional" if todos_no_ar else "instabilidade",
        "sistemas": lista_sistemas,
    }


def publicar_status_json(
    status_dados: dict[str, Any],
    bucket_name: Optional[str] = None,
    s3_client: Optional[Any] = None,
    nome_arquivo: str = "status.json",
) -> bool:
    """Faz upload do status.json estático para o bucket (S3 ou R2)."""
    bucket = bucket_name or os.environ.get("BUCKET_NAME")
    if not bucket:
        logger.warning("BUCKET_NAME não configurado; upload de status.json ignorado.")
        return False

    if s3_client is None:
        try:
            s3_client = obter_cliente_s3()
        except Exception as err:
            logger.error("Erro ao inicializar cliente de armazenamento: %s", err)
            return False

    corpo_bytes = json.dumps(status_dados, indent=2, ensure_ascii=False).encode("utf-8")

    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=nome_arquivo,
            Body=corpo_bytes,
            ContentType="application/json; charset=utf-8",
            CacheControl="public, max-age=60, s-maxage=60",
        )
        logger.info("status.json publicado com sucesso no bucket %s (%d bytes)", bucket, len(corpo_bytes))
        return True
    except Exception as err:
        logger.error("Falha ao publicar %s no bucket %s: %s", nome_arquivo, bucket, err)
        return False
