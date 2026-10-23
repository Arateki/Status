"""Avaliador de estado, regras de queda/volta e cálculo de disponibilidade."""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from .checker import ResultadoChecagem

ACAO_NENHUMA = "nenhuma"
ACAO_NOTIFICAR_QUEDA = "notificar_queda"
ACAO_NOTIFICAR_VOLTA = "notificar_volta"


def formatar_duracao(segundos: int) -> str:
    """Formata duração em segundos para texto legível em português."""
    if segundos < 0:
        segundos = 0

    if segundos < 60:
        return f"{segundos}s"

    minutos = segundos // 60
    resto_seg = segundos % 60
    if minutos < 60:
        return f"{minutos}m {resto_seg}s" if resto_seg > 0 else f"{minutos}m"

    horas = minutos // 60
    resto_min = minutos % 60
    if horas < 24:
        return f"{horas}h {resto_min}m" if resto_min > 0 else f"{horas}h"

    dias = horas // 24
    resto_horas = horas % 24
    return f"{dias}d {resto_horas}h" if resto_horas > 0 else f"{dias}d"


def _parse_iso(iso_str: str) -> datetime:
    """Converte string ISO 8601 em objeto datetime com fuso UTC."""
    # Compatível com formatos terminados em 'Z' ou offset '+00:00'
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class EstadoSistema:
    sistema_id: str
    status: str = "no_ar"  # "no_ar" ou "fora"
    falhas_consecutivas: int = 0
    ultimo_notificado: str = "no_ar"  # "no_ar" ou "fora"
    ultima_checagem: Optional[str] = None
    ultimo_codigo_http: Optional[int] = None
    ultimo_tempo_ms: Optional[int] = None
    queda_ativa: Optional[dict[str, Any]] = None  # {inicio, motivo}
    quedas_recentes: list[dict[str, Any]] = field(default_factory=list)
    baldes_24h: list[dict[str, Any]] = field(default_factory=list)  # baldes de 5 min (em memória / montado dos baldes por hora)
    historico_dias: dict[str, dict[str, int]] = field(default_factory=dict)  # "YYYY-MM-DD": {"total": X, "ok": Y}
    versao: int = 1
    alerta_pendente: Optional[dict[str, Any]] = None
    verificacao_indisponivel: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EstadoSistema":
        return cls(
            sistema_id=data.get("sistema_id", ""),
            status=data.get("status", "no_ar"),
            falhas_consecutivas=int(data.get("falhas_consecutivas", 0)),
            ultimo_notificado=data.get("ultimo_notificado", "no_ar"),
            ultima_checagem=data.get("ultima_checagem"),
            ultimo_codigo_http=data.get("ultimo_codigo_http"),
            ultimo_tempo_ms=data.get("ultimo_tempo_ms"),
            queda_ativa=data.get("queda_ativa"),
            quedas_recentes=list(data.get("quedas_recentes", [])),
            baldes_24h=list(data.get("baldes_24h", [])),
            historico_dias=dict(data.get("historico_dias", {})),
            versao=int(data.get("versao", 1)),
            alerta_pendente=data.get("alerta_pendente"),
            verificacao_indisponivel=bool(data.get("verificacao_indisponivel", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sistema_id": self.sistema_id,
            "status": self.status,
            "falhas_consecutivas": self.falhas_consecutivas,
            "ultimo_notificado": self.ultimo_notificado,
            "ultima_checagem": self.ultima_checagem,
            "ultimo_codigo_http": self.ultimo_codigo_http,
            "ultimo_tempo_ms": self.ultimo_tempo_ms,
            "queda_ativa": self.queda_ativa,
            "quedas_recentes": self.quedas_recentes,
            "baldes_24h": self.baldes_24h,
            "historico_dias": self.historico_dias,
            "versao": self.versao,
            "alerta_pendente": self.alerta_pendente,
            "verificacao_indisponivel": self.verificacao_indisponivel,
        }

    def to_state_dict(self) -> dict[str, Any]:
        """Retorna o dicionário compacto para o item STATE no DynamoDB (sem os baldes de 24h)."""
        d = self.to_dict()
        d.pop("baldes_24h", None)
        return d

    def registrar_alerta_enviado(self) -> None:
        """Confirma a entrega do alerta pendente e sincroniza ultimo_notificado."""
        if self.alerta_pendente:
            acao = self.alerta_pendente.get("acao")
            if acao == ACAO_NOTIFICAR_QUEDA:
                self.ultimo_notificado = "fora"
            elif acao == ACAO_NOTIFICAR_VOLTA:
                self.ultimo_notificado = "no_ar"
            self.alerta_pendente = None


def atualizar_baldes_24h(
    baldes: list[dict[str, Any]],
    timestamp_iso: str,
    ok: bool,
    latencia_ms: Optional[int],
) -> list[dict[str, Any]]:
    """Atualiza a lista de baldes de 5 minutos cobrindo até 24h (288 baldes)."""
    dt = _parse_iso(timestamp_iso)
    epoch_segundos = int(dt.timestamp())
    balde_inicio = epoch_segundos - (epoch_segundos % 300)
    balde_iso = datetime.fromtimestamp(balde_inicio, tz=timezone.utc).isoformat()

    novos_baldes = list(baldes)

    if novos_baldes and novos_baldes[-1].get("inicio") == balde_iso:
        # Atualiza o balde corrente
        b = novos_baldes[-1]
        qtd = b.get("total", 1)
        qtd_ok = b.get("total_ok", qtd if b.get("ok", True) else 0)
        soma_lat = b.get("soma_latencia", b.get("latencia_ms", 0) * qtd)
        if latencia_ms is not None:
            soma_lat += latencia_ms
            b["soma_latencia"] = soma_lat
            b["latencia_ms"] = int(round(soma_lat / (qtd + 1)))
        b["total"] = qtd + 1
        b["total_ok"] = qtd_ok + (1 if ok else 0)
        if not ok:
            b["ok"] = False
    else:
        # Novo balde de 5 min
        novos_baldes.append({
            "inicio": balde_iso,
            "latencia_ms": latencia_ms or 0,
            "soma_latencia": latencia_ms or 0,
            "ok": ok,
            "total": 1,
            "total_ok": 1 if ok else 0,
        })

    # Mantém apenas os últimos 288 baldes (24 horas = 288 * 5 min)
    limite_epoch = epoch_segundos - (24 * 3600)
    baldes_filtrados = [
        b for b in novos_baldes
        if int(_parse_iso(b["inicio"]).timestamp()) >= limite_epoch
    ]
    return baldes_filtrados[-288:]


def atualizar_historico_dias(
    historico: dict[str, dict[str, int]],
    timestamp_iso: str,
    ok: bool,
) -> dict[str, dict[str, int]]:
    """Atualiza agregados diários mantendo janela de 90 dias."""
    dt = _parse_iso(timestamp_iso)
    dia_chave = dt.strftime("%Y-%m-%d")

    novo_hist = {k: dict(v) for k, v in historico.items()}
    dia_dado = novo_hist.get(dia_chave, {"total": 0, "ok": 0})
    dia_dado["total"] = dia_dado.get("total", 0) + 1
    if ok:
        dia_dado["ok"] = dia_dado.get("ok", 0) + 1
    novo_hist[dia_chave] = dia_dado

    # Remove dias mais antigos que 90 dias
    limite_dia = (dt - timedelta(days=90)).strftime("%Y-%m-%d")
    return {k: v for k, v in novo_hist.items() if k >= limite_dia}


def calcular_porcentagens(
    historico_dias: dict[str, dict[str, int]],
    baldes_24h: list[dict[str, Any]],
    timestamp_referencia: Optional[str] = None,
) -> dict[str, float]:
    """Calcula disponibilidade percentual em 24h, 7d, 30d e 90d."""
    ref_dt = _parse_iso(timestamp_referencia) if timestamp_referencia else datetime.now(timezone.utc)

    # 24 Horas: calculado a partir dos baldes das últimas 24h
    if baldes_24h:
        total_24h = sum(b.get("total", 1) for b in baldes_24h)
        total_ok_24h = sum(b.get("total_ok", b.get("total", 1) if b.get("ok", True) else 0) for b in baldes_24h)
        pct_24h = round((total_ok_24h / total_24h) * 100.0, 2) if total_24h > 0 else 100.0
    else:
        pct_24h = 100.0

    def calc_janela_dias(dias: int) -> float:
        # Janela de N dias inclui hoje mais os (N-1) dias anteriores
        limite = (ref_dt - timedelta(days=max(0, dias - 1))).strftime("%Y-%m-%d")
        total = 0
        total_ok = 0
        for dia, cont in historico_dias.items():
            if dia >= limite:
                total += cont.get("total", 0)
                total_ok += cont.get("ok", 0)
        if total == 0:
            return 100.0
        return round((total_ok / total) * 100.0, 2)

    return {
        "h24": pct_24h,
        "d7": calc_janela_dias(7),
        "d30": calc_janela_dias(30),
        "d90": calc_janela_dias(90),
    }


def gerar_dias_90(
    historico_dias: dict[str, dict[str, int]],
    timestamp_referencia: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Gera a lista contínua dos últimos 90 dias com percentual, minutos fora e status para a barra de 90 segmentos."""
    ref_dt = _parse_iso(timestamp_referencia) if timestamp_referencia else datetime.now(timezone.utc)
    hoje = ref_dt.date()

    dias_lista: list[dict[str, Any]] = []
    # De 89 dias atrás até hoje (total de 90 dias cronológicos)
    for i in range(89, -1, -1):
        dia_dt = hoje - timedelta(days=i)
        dia_str = dia_dt.strftime("%Y-%m-%d")
        cont = historico_dias.get(dia_str)

        if not cont or cont.get("total", 0) == 0:
            dias_lista.append({
                "data": dia_str,
                "pct": None,
                "minutos_fora": 0,
                "status": "sem_dados",
            })
        else:
            total = cont["total"]
            ok = cont.get("ok", 0)
            minutos_fora = max(0, total - ok)
            pct = round((ok / total) * 100.0, 2)

            if ok == total:
                status_dia = "verde"
            elif pct >= 95.0:
                status_dia = "parcial"
            else:
                status_dia = "queda"

            dias_lista.append({
                "data": dia_str,
                "pct": pct,
                "minutos_fora": minutos_fora,
                "status": status_dia,
            })

    return dias_lista


def avaliar_checagem(
    estado_anterior: Optional[EstadoSistema],
    resultado: ResultadoChecagem,
) -> tuple[EstadoSistema, str, Optional[dict[str, Any]]]:
    """Aplica as regras de negócio sobre a checagem:

    - Regra das 2 falhas para queda
    - Volta imediata com 1 checagem ok
    - 403 do Cloudflare tratado como erro do checador (não altera estado)

    Retorna: (novo_estado, acao_notificacao, dados_evento)
    onde acao_notificacao pode ser 'nenhuma', 'notificar_queda' ou 'notificar_volta'.
    """
    estado = (
        EstadoSistema.from_dict(estado_anterior.to_dict())
        if estado_anterior
        else EstadoSistema(sistema_id=resultado.sistema_id)
    )
    if estado_anterior:
        estado.versao = estado_anterior.versao + 1
    else:
        estado.versao = 1

    estado.ultima_checagem = resultado.timestamp
    estado.ultimo_codigo_http = resultado.codigo_http
    if resultado.latencia_ms is not None:
        estado.ultimo_tempo_ms = resultado.latencia_ms

    # Se foi erro do checador (ex: Cloudflare bloqueando robô genérico),
    # não é falha do sistema vigiado, mas indica verificação indisponível.
    if resultado.erro_checador:
        estado.verificacao_indisponivel = True
        return estado, ACAO_NENHUMA, None

    estado.verificacao_indisponivel = False

    # Atualiza baldes de 24h e histórico diário
    estado.baldes_24h = atualizar_baldes_24h(
        estado.baldes_24h,
        resultado.timestamp,
        resultado.ok,
        resultado.latencia_ms,
    )
    estado.historico_dias = atualizar_historico_dias(
        estado.historico_dias,
        resultado.timestamp,
        resultado.ok,
    )

    if resultado.ok:
        # Checagem com sucesso
        estado.falhas_consecutivas = 0

        # Se estava fora, recuperou! (Regra: volta = 1 checagem ok)
        if estado.status == "fora":
            estado.status = "no_ar"
            agora_dt = _parse_iso(resultado.timestamp)

            dados_queda = estado.queda_ativa or {}
            inicio_str = dados_queda.get("inicio", resultado.timestamp)
            inicio_dt = _parse_iso(inicio_str)
            duracao_seg = max(0, int((agora_dt - inicio_dt).total_seconds()))

            registro_queda = {
                "inicio": inicio_str,
                "fim": resultado.timestamp,
                "duracao_segundos": duracao_seg,
                "duracao_texto": formatar_duracao(duracao_seg),
                "motivo": dados_queda.get("motivo", "Falha de resposta"),
            }

            # Adiciona ao topo das quedas recentes (máx 15)
            estado.quedas_recentes = [registro_queda] + estado.quedas_recentes[:14]
            estado.queda_ativa = None

            # Dispara alerta de volta se já tinha notificado a queda
            if estado.ultimo_notificado == "fora":
                dados_volta = {
                    "sistema_id": resultado.sistema_id,
                    "nome": resultado.nome,
                    "url": resultado.url,
                    "inicio": inicio_str,
                    "fim": resultado.timestamp,
                    "duracao_texto": formatar_duracao(duracao_seg),
                }
                estado.alerta_pendente = {
                    "acao": ACAO_NOTIFICAR_VOLTA,
                    "dados": dados_volta,
                    "criado_em": resultado.timestamp,
                }
                # Não altera ultimo_notificado aqui; só é atualizado após persistência e envio
                return estado, ACAO_NOTIFICAR_VOLTA, dados_volta
            else:
                estado.alerta_pendente = None

        return estado, ACAO_NENHUMA, None

    else:
        # Checagem falhou
        estado.falhas_consecutivas += 1

        if estado.falhas_consecutivas == 1:
            # 1 falha: soluço de rede, NÃO vira queda
            # Mantém status 'no_ar'
            return estado, ACAO_NENHUMA, None

        elif estado.falhas_consecutivas == 2:
            # 2 falhas consecutivas: QUEDA CONFIRMADA!
            estado.status = "fora"
            motivo = resultado.erro or "Falha de resposta"
            estado.queda_ativa = {
                "inicio": resultado.timestamp,
                "motivo": motivo,
            }

            # Se ainda não notificou queda, prepara alerta pendente
            if estado.ultimo_notificado != "fora":
                dados_queda = {
                    "sistema_id": resultado.sistema_id,
                    "nome": resultado.nome,
                    "url": resultado.url,
                    "inicio": resultado.timestamp,
                    "motivo": motivo,
                    "codigo_http": resultado.codigo_http,
                }
                estado.alerta_pendente = {
                    "acao": ACAO_NOTIFICAR_QUEDA,
                    "dados": dados_queda,
                    "criado_em": resultado.timestamp,
                }
                # Não altera ultimo_notificado aqui; só é atualizado após persistência e envio
                return estado, ACAO_NOTIFICAR_QUEDA, dados_queda

            return estado, ACAO_NENHUMA, None

        else:
            # Continua fora (> 2 falhas). Já notificado ou pendente de envio; não dispara nova ação
            estado.status = "fora"
            return estado, ACAO_NENHUMA, None
