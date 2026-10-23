"""Testes unitários para regras de queda, recuperação e cálculos estatísticos."""

from datetime import datetime, timezone, timedelta
import unittest

from checador.checker import ResultadoChecagem
from checador.evaluator import (
    ACAO_NENHUMA,
    ACAO_NOTIFICAR_QUEDA,
    ACAO_NOTIFICAR_VOLTA,
    EstadoSistema,
    atualizar_baldes_24h,
    atualizar_historico_dias,
    avaliar_checagem,
    calcular_porcentagens,
    formatar_duracao,
)


class TestEvaluator(unittest.TestCase):
    def setUp(self):
        self.agora = datetime.now(timezone.utc)
        self.agora_iso = self.agora.isoformat()

    def test_regra_duas_falhas_para_queda(self):
        """Um soluço pontual (1 falha) não derruba o sistema; 2 falhas confirmam a queda."""
        estado_inicial = EstadoSistema(sistema_id="arateki-site", status="no_ar")

        # 1ª falha (soluço de rede)
        res1 = ResultadoChecagem(
            sistema_id="arateki-site",
            nome="Site da Arateki",
            url="https://arateki.com",
            timestamp=self.agora_iso,
            ok=False,
            codigo_http=500,
            latencia_ms=120,
            erro="HTTP 500",
        )
        estado1, acao1, dados1 = avaliar_checagem(estado_inicial, res1)

        self.assertEqual(estado1.status, "no_ar", "1 falha isolada não deve marcar como fora")
        self.assertEqual(estado1.falhas_consecutivas, 1)
        self.assertEqual(acao1, ACAO_NENHUMA)
        self.assertIsNone(dados1)

        # 2ª falha consecutiva (confirmação da queda)
        proximo_iso = (self.agora + timedelta(minutes=1)).isoformat()
        res2 = ResultadoChecagem(
            sistema_id="arateki-site",
            nome="Site da Arateki",
            url="https://arateki.com",
            timestamp=proximo_iso,
            ok=False,
            codigo_http=503,
            latencia_ms=150,
            erro="HTTP 503",
        )
        estado2, acao2, dados2 = avaliar_checagem(estado1, res2)

        self.assertEqual(estado2.status, "fora", "2 falhas consecutivas devem marcar como fora")
        self.assertEqual(estado2.falhas_consecutivas, 2)
        self.assertEqual(acao2, ACAO_NOTIFICAR_QUEDA)
        self.assertIsNotNone(dados2)
        self.assertEqual(dados2["sistema_id"], "arateki-site")
        self.assertEqual(dados2["motivo"], "HTTP 503")

        # Simula entrega e confirmação do alerta SNS
        self.assertEqual(estado2.ultimo_notificado, "no_ar", "Ainda não confirmado")
        estado2.registrar_alerta_enviado()
        self.assertEqual(estado2.ultimo_notificado, "fora", "Confirmado após SNS")

        # 3ª falha consecutiva (continua fora, mas NÃO renotifica)
        depois_iso = (self.agora + timedelta(minutes=2)).isoformat()
        res3 = ResultadoChecagem(
            sistema_id="arateki-site",
            nome="Site da Arateki",
            url="https://arateki.com",
            timestamp=depois_iso,
            ok=False,
            codigo_http=503,
            latencia_ms=160,
            erro="HTTP 503",
        )
        estado3, acao3, dados3 = avaliar_checagem(estado2, res3)

        self.assertEqual(estado3.status, "fora")
        self.assertEqual(estado3.falhas_consecutivas, 3)
        self.assertEqual(acao3, ACAO_NENHUMA, "Não deve disparar alerta repetido enquanto persistir em queda")

    def test_volta_com_uma_checagem_ok(self):
        """Um sistema que estava fora deve voltar imediatamente com 1 checagem OK."""
        # Sistema fora e já notificado
        inicio_queda = (self.agora - timedelta(minutes=15)).isoformat()
        estado_fora = EstadoSistema(
            sistema_id="keyflow-api",
            status="fora",
            falhas_consecutivas=15,
            ultimo_notificado="fora",
            queda_ativa={"inicio": inicio_queda, "motivo": "HTTP 500"},
        )

        res_ok = ResultadoChecagem(
            sistema_id="keyflow-api",
            nome="API do KeyFlow",
            url="https://keyflow-api.arateki.com/health",
            timestamp=self.agora_iso,
            ok=True,
            codigo_http=200,
            latencia_ms=45,
            erro=None,
        )

        novo_estado, acao, dados = avaliar_checagem(estado_fora, res_ok)

        self.assertEqual(novo_estado.status, "no_ar", "Deve recuperar para no_ar com 1 checagem ok")
        self.assertEqual(novo_estado.falhas_consecutivas, 0)
        self.assertEqual(novo_estado.alerta_pendente["acao"], ACAO_NOTIFICAR_VOLTA)
        self.assertEqual(novo_estado.ultimo_notificado, "fora", "Permanece fora até confirmação do SNS")
        novo_estado.registrar_alerta_enviado()
        self.assertEqual(novo_estado.ultimo_notificado, "no_ar", "Atualizado após entrega confirmada")
        self.assertEqual(acao, ACAO_NOTIFICAR_VOLTA)
        self.assertIsNotNone(dados)
        self.assertIn("15m", dados["duracao_texto"])
        self.assertIsNone(novo_estado.queda_ativa)
        self.assertEqual(len(novo_estado.quedas_recentes), 1)
        self.assertEqual(novo_estado.quedas_recentes[0]["inicio"], inicio_queda)
        self.assertEqual(novo_estado.quedas_recentes[0]["fim"], self.agora_iso)

    def test_erro_checador_cloudflare_nao_derruba_sistema(self):
        """Erro 403 do Cloudflare bloqueando robô não deve contar como queda do sistema."""
        estado_inicial = EstadoSistema(sistema_id="keyflow-api", status="no_ar", falhas_consecutivas=0)

        res_cf = ResultadoChecagem(
            sistema_id="keyflow-api",
            nome="API do KeyFlow",
            url="https://keyflow-api.arateki.com/health",
            timestamp=self.agora_iso,
            ok=False,
            codigo_http=403,
            latencia_ms=80,
            erro="Cloudflare 403 (bloqueio do checador)",
            erro_checador=True,
        )

        estado1, acao1, _ = avaliar_checagem(estado_inicial, res_cf)
        self.assertEqual(estado1.status, "no_ar")
        self.assertEqual(estado1.falhas_consecutivas, 0, "Erro do checador não incrementa falhas do sistema")
        self.assertEqual(acao1, ACAO_NENHUMA)

    def test_calculo_porcentagens_disponibilidade(self):
        """Verifica o cálculo de % nas janelas de 24h, 7d, 30d e 90d."""
        ref_dt = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        ref_iso = ref_dt.isoformat()

        # Baldes de 24h: 90 ok e 10 falhas = 90%
        baldes_24h = [
            {"inicio": (ref_dt - timedelta(minutes=5 * i)).isoformat(), "total": 1, "ok": (i >= 10)}
            for i in range(100)
        ]

        # Histórico de dias:
        historico_dias = {}
        for d in range(100):
            dia_str = (ref_dt - timedelta(days=d)).strftime("%Y-%m-%d")
            # Primeiros 7 dias: 100% ok (100 total, 100 ok)
            if d < 7:
                historico_dias[dia_str] = {"total": 100, "ok": 100}
            # Dias 7 a 30: 95% ok (100 total, 95 ok)
            elif d < 30:
                historico_dias[dia_str] = {"total": 100, "ok": 95}
            # Dias 30 a 90: 90% ok (100 total, 90 ok)
            elif d < 90:
                historico_dias[dia_str] = {"total": 100, "ok": 90}
            else:
                historico_dias[dia_str] = {"total": 100, "ok": 0}

        pcts = calcular_porcentagens(historico_dias, baldes_24h, timestamp_referencia=ref_iso)

        self.assertEqual(pcts["h24"], 90.0)
        self.assertEqual(pcts["d7"], 100.0)
        self.assertAlmostEqual(pcts["d30"], 96.17, places=1)
        self.assertAlmostEqual(pcts["d90"], 92.05, places=1)

    def test_formatar_duracao(self):
        """Verifica a formatação amigável de durações."""
        self.assertEqual(formatar_duracao(45), "45s")
        self.assertEqual(formatar_duracao(120), "2m")
        self.assertEqual(formatar_duracao(135), "2m 15s")
        self.assertEqual(formatar_duracao(3600), "1h")
        self.assertEqual(formatar_duracao(3720), "1h 2m")
        self.assertEqual(formatar_duracao(86400), "1d")
        self.assertEqual(formatar_duracao(90000), "1d 1h")


if __name__ == "__main__":
    unittest.main()
