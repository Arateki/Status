"""Módulo de checagem HTTP em paralelo para o monitor de status."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .config import TIMEOUT_PADRAO, USER_AGENT


@dataclass
class ResultadoChecagem:
    sistema_id: str
    nome: str
    url: str
    timestamp: str  # ISO 8601 UTC
    ok: bool
    codigo_http: Optional[int]
    latencia_ms: Optional[int]
    erro: Optional[str]
    erro_checador: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _eh_bloqueio_cloudflare(err: urllib.error.HTTPError) -> bool:
    """Verifica se o erro 403 partiu de um bloqueio do Cloudflare contra o robô."""
    if err.code != 403:
        return False

    headers = err.headers or {}
    server = headers.get("Server", "").lower()
    cf_ray = headers.get("cf-ray", "")
    cf_mitigated = headers.get("cf-mitigated", "")

    if "cloudflare" in server or cf_ray or cf_mitigated:
        return True

    # Verifica também indícios no corpo retornado (até 1 KB)
    try:
        corpo = err.read(1024).decode("utf-8", errors="ignore").lower()
        if "cloudflare" in corpo or "attention required" in corpo:
            return True
    except Exception:
        pass

    return False


def checar_sistema(
    sistema: dict[str, Any],
    timeout: int = TIMEOUT_PADRAO,
    user_agent: str = USER_AGENT,
) -> ResultadoChecagem:
    """Executa a checagem HTTP de um sistema individual."""
    sistema_id = sistema["id"]
    nome = sistema.get("nome", sistema_id)
    url = sistema["url"]
    espera_codigo = sistema.get("espera_codigo", 200)
    espera_corpo = sistema.get("espera_corpo")

    agora_iso = datetime.now(timezone.utc).isoformat()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "*/*",
        },
    )

    inicio = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            duracao_seg = time.perf_counter() - inicio
            latencia_ms = int(round(duracao_seg * 1000))
            codigo_http = resp.status

            if codigo_http != espera_codigo:
                return ResultadoChecagem(
                    sistema_id=sistema_id,
                    nome=nome,
                    url=url,
                    timestamp=agora_iso,
                    ok=False,
                    codigo_http=codigo_http,
                    latencia_ms=latencia_ms,
                    erro=f"HTTP {codigo_http} (esperado {espera_codigo})",
                    erro_checador=False,
                )

            if espera_corpo:
                corpo_bytes = resp.read(4096)
                corpo_texto = corpo_bytes.decode("utf-8", errors="ignore")
                if espera_corpo not in corpo_texto:
                    return ResultadoChecagem(
                        sistema_id=sistema_id,
                        nome=nome,
                        url=url,
                        timestamp=agora_iso,
                        ok=False,
                        codigo_http=codigo_http,
                        latencia_ms=latencia_ms,
                        erro=f"Corpo não contém '{espera_corpo}'",
                        erro_checador=False,
                    )

            return ResultadoChecagem(
                sistema_id=sistema_id,
                nome=nome,
                url=url,
                timestamp=agora_iso,
                ok=True,
                codigo_http=codigo_http,
                latencia_ms=latencia_ms,
                erro=None,
                erro_checador=False,
            )

    except urllib.error.HTTPError as err:
        duracao_seg = time.perf_counter() - inicio
        latencia_ms = int(round(duracao_seg * 1000))
        if _eh_bloqueio_cloudflare(err):
            return ResultadoChecagem(
                sistema_id=sistema_id,
                nome=nome,
                url=url,
                timestamp=agora_iso,
                ok=False,
                codigo_http=err.code,
                latencia_ms=latencia_ms,
                erro="Cloudflare 403 (bloqueio do checador)",
                erro_checador=True,
            )

        return ResultadoChecagem(
            sistema_id=sistema_id,
            nome=nome,
            url=url,
            timestamp=agora_iso,
            ok=False,
            codigo_http=err.code,
            latencia_ms=latencia_ms,
            erro=f"HTTP {err.code}",
            erro_checador=False,
        )

    except (urllib.error.URLError, TimeoutError, OSError) as err:
        duracao_seg = time.perf_counter() - inicio
        # Se for timeout
        motivo_str = str(err).lower()
        if isinstance(err, TimeoutError) or "timed out" in motivo_str or "timeout" in motivo_str:
            return ResultadoChecagem(
                sistema_id=sistema_id,
                nome=nome,
                url=url,
                timestamp=agora_iso,
                ok=False,
                codigo_http=None,
                latencia_ms=None,
                erro=f"Timeout ({timeout}s)",
                erro_checador=False,
            )

        return ResultadoChecagem(
            sistema_id=sistema_id,
            nome=nome,
            url=url,
            timestamp=agora_iso,
            ok=False,
            codigo_http=None,
            latencia_ms=None,
            erro="Falha de conexão",
            erro_checador=False,
        )

    except Exception as err:
        return ResultadoChecagem(
            sistema_id=sistema_id,
            nome=nome,
            url=url,
            timestamp=agora_iso,
            ok=False,
            codigo_http=None,
            latencia_ms=None,
            erro=f"Erro inesperado: {type(err).__name__}",
            erro_checador=False,
        )


def checar_todos(
    sistemas: list[dict[str, Any]],
    timeout: int = TIMEOUT_PADRAO,
    user_agent: str = USER_AGENT,
    max_workers: Optional[int] = None,
) -> list[ResultadoChecagem]:
    """Executa a checagem de todos os sistemas em paralelo.

    O tempo total de execução é o do sistema mais lento.
    """
    if not sistemas:
        return []

    workers = max_workers or len(sistemas)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futuros = [
            executor.submit(checar_sistema, s, timeout=timeout, user_agent=user_agent)
            for s in sistemas
        ]
        return [f.result() for f in futuros]
