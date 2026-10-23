"""Módulo de persistência no DynamoDB e publicação do status.json no S3/R2."""

from datetime import datetime, timezone, timedelta
import json
import logging
import os
import time
from typing import Any, Optional

from .evaluator import EstadoSistema, _parse_iso, calcular_porcentagens, gerar_dias_90

logger = logging.getLogger(__name__)

TTL_90_DIAS_SEGUNDOS = 90 * 86400


def _obter_boto_config():
    """Retorna configuração de timeout de 3s/5s para clientes boto3 se botocore estiver disponível."""
    try:
        from botocore.config import Config
        return Config(
            connect_timeout=3,
            read_timeout=5,
            retries={"max_attempts": 2, "mode": "standard"},
        )
    except ImportError:
        return None


class ErroLeituraDynamoDB(Exception):
    """Lançada quando a consulta ao DynamoDB falha."""
    pass


class ErroGravacaoDynamoDB(Exception):
    """Lançada quando a persistência no DynamoDB falha."""
    pass


class ErroPublicacaoR2(Exception):
    """Lançada quando o upload do status.json no Cloudflare R2 / S3 falha."""
    pass


def obter_cliente_dynamodb():
    """Retorna cliente DynamoDB do boto3 com timeout estrito de 3s/5s."""
    import boto3

    cfg = _obter_boto_config()
    kwargs = {"config": cfg} if cfg else {}
    return boto3.client("dynamodb", **kwargs)


def obter_cliente_s3(
    endpoint_url: Optional[str] = None,
    access_key_id: Optional[str] = None,
    secret_access_key: Optional[str] = None,
):
    """Retorna cliente S3 do boto3 configurado para o Cloudflare R2 com timeout estrito."""
    import boto3

    ep = endpoint_url or os.environ.get("R2_ENDPOINT_URL") or os.environ.get("S3_ENDPOINT_URL")
    key = access_key_id or os.environ.get("R2_ACCESS_KEY_ID")
    secret = secret_access_key or os.environ.get("R2_SECRET_ACCESS_KEY")

    cfg = _obter_boto_config()
    kwargs: dict[str, Any] = {"config": cfg} if cfg else {}
    if ep:
        kwargs["endpoint_url"] = ep
        # O R2 só aceita a região "auto"; sem isto o boto3 usa a região da Lambda e o PutObject falha.
        kwargs["region_name"] = "auto"
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


def carregar_baldes_24h(
    sistema_id: str,
    table_name: Optional[str] = None,
    dynamo_client: Optional[Any] = None,
    agora_dt: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Carrega os baldes de 5 min das últimas 24h a partir dos itens de hora no DynamoDB."""
    tabela = table_name or os.environ.get("TABLE_NAME", "arateki-status")
    if dynamo_client is None:
        try:
            dynamo_client = obter_cliente_dynamodb()
        except Exception as err:
            raise ErroLeituraDynamoDB(f"Não foi possível inicializar cliente DynamoDB: {err}") from err

    ref = agora_dt or datetime.now(timezone.utc)
    hora_ini = (ref - timedelta(hours=25)).strftime("%Y-%m-%dT%H")
    hora_fim = ref.strftime("%Y-%m-%dT%H")

    try:
        resp = dynamo_client.query(
            TableName=tabela,
            KeyConditionExpression="PK = :pk AND SK BETWEEN :h_ini AND :h_fim",
            ExpressionAttributeValues={
                ":pk": {"S": sistema_id},
                ":h_ini": {"S": f"HOUR#{hora_ini}"},
                ":h_fim": {"S": f"HOUR#{hora_fim}"},
            },
            ConsistentRead=False,
        )
    except Exception as err:
        logger.warning("Não foi possível consultar baldes por hora para %s: %s", sistema_id, err)
        return []

    items = resp.get("Items", [])
    todos_baldes: list[dict[str, Any]] = []
    for item in items:
        if "baldes_json" in item:
            try:
                b_lista = json.loads(item["baldes_json"]["S"])
                if isinstance(b_lista, list):
                    todos_baldes.extend(b_lista)
            except Exception:
                pass

    limite_epoch = int(ref.timestamp()) - (24 * 3600)
    baldes_validos: list[dict[str, Any]] = []
    for b in todos_baldes:
        inicio_str = b.get("inicio")
        if inicio_str:
            try:
                if int(_parse_iso(inicio_str).timestamp()) >= limite_epoch:
                    baldes_validos.append(b)
            except Exception:
                pass

    baldes_validos.sort(key=lambda x: x.get("inicio", ""))
    return baldes_validos[-288:]


def salvar_balde_hora(
    sistema_id: str,
    timestamp_iso: str,
    baldes_24h: list[dict[str, Any]],
    table_name: Optional[str] = None,
    dynamo_client: Optional[Any] = None,
) -> None:
    """Salva apenas os baldes pertencentes à hora corrente em um item dedicado HOUR#YYYY-MM-DDTHH."""
    tabela = table_name or os.environ.get("TABLE_NAME", "arateki-status")
    if dynamo_client is None:
        dynamo_client = obter_cliente_dynamodb()

    dt = _parse_iso(timestamp_iso)
    chave_hora = dt.strftime("%Y-%m-%dT%H")

    # Filtra baldes desta hora
    baldes_hora = [
        b for b in baldes_24h
        if b.get("inicio", "").startswith(chave_hora)
    ]
    if not baldes_hora:
        return

    ttl_26h = int(time.time()) + (26 * 3600)  # Expira após 26 horas
    baldes_json = json.dumps(baldes_hora)

    try:
        dynamo_client.put_item(
            TableName=tabela,
            Item={
                "PK": {"S": sistema_id},
                "SK": {"S": f"HOUR#{chave_hora}"},
                "baldes_json": {"S": baldes_json},
                "qtd_baldes": {"N": str(len(baldes_hora))},
                "ttl": {"N": str(ttl_26h)},
            },
        )
    except Exception as err:
        logger.warning("Falha ao salvar balde de hora %s para %s: %s", chave_hora, sistema_id, err)
        raise


def salvar_estado_e_checagem(
    estado: EstadoSistema,
    resultado_checagem: dict[str, Any],
    versao_esperada: Optional[int] = None,
    table_name: Optional[str] = None,
    dynamo_client: Optional[Any] = None,
) -> bool:
    """Salva o estado consolidado (compacto, sem baldes 24h), os baldes da hora e a checagem bruta.

    Utiliza escrita condicional por versão para evitar sobrescrita concorrente.
    Lança ErroGravacaoDynamoDB em caso de falha.
    """
    tabela = table_name or os.environ.get("TABLE_NAME", "arateki-status")
    if dynamo_client is None:
        try:
            dynamo_client = obter_cliente_dynamodb()
        except Exception as err:
            logger.error("Não foi possível conectar ao DynamoDB para salvar: %s", err)
            raise ErroGravacaoDynamoDB(f"Cliente DynamoDB indisponível: {err}") from err

    agora_epoch = int(time.time())
    ttl_epoch = agora_epoch + TTL_90_DIAS_SEGUNDOS
    timestamp_iso = resultado_checagem.get("timestamp", datetime.now(timezone.utc).isoformat())

    try:
        # 1. Salva o item STATE compacto (sem baldes_24h) com escrita condicional de versão
        estado_json = json.dumps(estado.to_state_dict())
        put_kwargs: dict[str, Any] = {
            "TableName": tabela,
            "Item": {
                "PK": {"S": estado.sistema_id},
                "SK": {"S": "STATE"},
                "status": {"S": estado.status},
                "falhas_consecutivas": {"N": str(estado.falhas_consecutivas)},
                "ultimo_notificado": {"S": estado.ultimo_notificado},
                "ultima_checagem": {"S": estado.ultima_checagem or timestamp_iso},
                "versao": {"N": str(estado.versao)},
                "dados_json": {"S": estado_json},
            },
        }

        # Escrita condicional: se versão esperada informada, exige match ou inexistência do item
        if versao_esperada is not None:
            if versao_esperada == 0:
                put_kwargs["ConditionExpression"] = "attribute_not_exists(PK)"
            else:
                put_kwargs["ConditionExpression"] = (
                    "attribute_not_exists(PK) OR attribute_not_exists(versao) OR versao = :v_esp"
                )
                put_kwargs["ExpressionAttributeValues"] = {":v_esp": {"N": str(versao_esperada)}}

        dynamo_client.put_item(**put_kwargs)

        # 2. Salva os baldes da hora corrente em item dedicado (~800 bytes)
        salvar_balde_hora(
            sistema_id=estado.sistema_id,
            timestamp_iso=timestamp_iso,
            baldes_24h=estado.baldes_24h,
            table_name=tabela,
            dynamo_client=dynamo_client,
        )

        # 3. Salva o registro bruto da checagem com TTL de 90 dias
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
        raise ErroGravacaoDynamoDB(f"Falha ao persistir no DynamoDB para {estado.sistema_id}: {err}") from err


def confirmar_alerta_enviado(
    estado: EstadoSistema,
    table_name: Optional[str] = None,
    dynamo_client: Optional[Any] = None,
    max_tentativas: int = 3,
) -> bool:
    """Atualiza o DynamoDB após confirmação de entrega do alerta SNS.

    Atualiza ultimo_notificado, limpa alerta_pendente e incrementa versao.
    Executa com retentativa curta para tolerância a falhas transitórias.
    """
    tabela = table_name or os.environ.get("TABLE_NAME", "arateki-status")
    if dynamo_client is None:
        try:
            dynamo_client = obter_cliente_dynamodb()
        except Exception as err:
            logger.error("Cliente DynamoDB indisponível para confirmar alerta: %s", err)
            raise ErroGravacaoDynamoDB(f"Cliente DynamoDB indisponível: {err}") from err

    v_antiga = estado.versao
    estado.registrar_alerta_enviado()
    estado.versao = v_antiga + 1
    estado_json = json.dumps(estado.to_state_dict())

    item = {
        "PK": {"S": estado.sistema_id},
        "SK": {"S": "STATE"},
        "status": {"S": estado.status},
        "falhas_consecutivas": {"N": str(estado.falhas_consecutivas)},
        "ultimo_notificado": {"S": estado.ultimo_notificado},
        "ultima_checagem": {"S": estado.ultima_checagem or ""},
        "versao": {"N": str(estado.versao)},
        "dados_json": {"S": estado_json},
    }

    ultimo_erro = None
    for tentativa in range(max_tentativas):
        try:
            dynamo_client.put_item(
                TableName=tabela,
                Item=item,
                ConditionExpression="attribute_not_exists(PK) OR attribute_not_exists(versao) OR versao = :v_ant",
                ExpressionAttributeValues={":v_ant": {"N": str(v_antiga)}},
            )
            return True
        except Exception as err:
            ultimo_erro = err
            logger.warning(
                "Tentativa %d de confirmar alerta no DynamoDB para %s falhou: %s",
                tentativa + 1,
                estado.sistema_id,
                err,
            )
            if tentativa < max_tentativas - 1:
                time.sleep(0.05 * (2 ** tentativa))

    logger.error("Erro ao confirmar entrega de alerta no DynamoDB para %s após %d tentativas: %s", estado.sistema_id, max_tentativas, ultimo_erro)
    raise ErroGravacaoDynamoDB(f"Falha ao confirmar alerta de {estado.sistema_id}: {ultimo_erro}") from ultimo_erro


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

        is_indisponivel = bool(estado.verificacao_indisponivel)
        status_exibicao = "indisponivel" if is_indisponivel else estado.status

        if status_exibicao != "no_ar":
            todos_no_ar = False

        pcts = calcular_porcentagens(
            estado.historico_dias,
            estado.baldes_24h,
            timestamp_referencia=timestamp,
        )

        baldes_formatados = [
            {
                "inicio": b.get("inicio", ""),
                "latencia_ms": b.get("latencia_ms", 0),
                "ok": b.get("ok", True),
            }
            for b in estado.baldes_24h
        ]

        dias_90 = gerar_dias_90(estado.historico_dias, timestamp_referencia=timestamp)

        lista_sistemas.append({
            "id": sid,
            "nome": cfg.get("nome", sid),
            "url": cfg["url"],
            "status": status_exibicao,
            "verificacao_indisponivel": is_indisponivel,
            "ultima_checagem": estado.ultima_checagem or timestamp,
            "latencia_ms": estado.ultimo_tempo_ms,
            "codigo_http": estado.ultimo_codigo_http,
            "disponibilidade": pcts,
            "baldes_24h": baldes_formatados,
            "dias_90": dias_90,
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
    """Faz upload do status.json estático para o bucket (S3 ou R2).

    Lança ErroPublicacaoR2 se o upload falhar e o bucket estiver configurado.
    """
    bucket = bucket_name or os.environ.get("BUCKET_NAME")
    if not bucket:
        logger.warning("BUCKET_NAME não configurado; upload de status.json ignorado.")
        return False

    if s3_client is None:
        try:
            s3_client = obter_cliente_s3()
        except Exception as err:
            logger.error("Erro ao inicializar cliente de armazenamento: %s", err)
            raise ErroPublicacaoR2(f"Cliente S3/R2 indisponível: {err}") from err

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
        raise ErroPublicacaoR2(f"Falha ao enviar {nome_arquivo} para o bucket {bucket}: {err}") from err
