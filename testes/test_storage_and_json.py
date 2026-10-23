"""Testes unitários para geração do status.json e rollups de armazenamento."""

from datetime import datetime, timezone, timedelta
import json
import unittest

from checador.evaluator import (
    EstadoSistema,
    atualizar_baldes_24h,
    atualizar_historico_dias,
)
from checador.storage import construir_status_json


class TestStorageAndJson(unittest.TestCase):
    def test_construir_status_json_estrutura_e_sanitizacao(self):
        """Valida que status.json possui estrutura completa e nenhum dado sensível."""
        agora = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)
        agora_iso = agora.isoformat()

        sistemas_cfg = [
            {"id": "arateki-site", "nome": "Site da Arateki", "url": "https://arateki.com"},
            {"id": "raiznet-site", "nome": "Site da Raiznet", "url": "https://raiznet.com"},
        ]

        estados = {
            "arateki-site": EstadoSistema(
                sistema_id="arateki-site",
                status="no_ar",
                ultimo_codigo_http=200,
                ultimo_tempo_ms=35,
                baldes_24h=[{"inicio": agora_iso, "latencia_ms": 35, "ok": True}],
                historico_dias={"2026-10-23": {"total": 50, "ok": 50}},
            ),
            "raiznet-site": EstadoSistema(
                sistema_id="raiznet-site",
                status="fora",
                ultimo_codigo_http=500,
                ultimo_tempo_ms=120,
                quedas_recentes=[{
                    "inicio": (agora - timedelta(minutes=10)).isoformat(),
                    "fim": agora_iso,
                    "duracao_segundos": 600,
                    "duracao_texto": "10m",
                    "motivo": "HTTP 500",
                }],
            ),
        }

        status_json = construir_status_json(sistemas_cfg, estados, agora_iso=agora_iso)

        self.assertEqual(status_json["gerado_em"], agora_iso)
        self.assertEqual(status_json["status_geral"], "instabilidade", "Deve acusar instabilidade se um estiver fora")
        self.assertEqual(len(status_json["sistemas"]), 2)

        s1 = status_json["sistemas"][0]
        self.assertEqual(s1["id"], "arateki-site")
        self.assertEqual(s1["status"], "no_ar")
        self.assertEqual(s1["latencia_ms"], 35)
        self.assertEqual(s1["disponibilidade"]["h24"], 100.0)

        s2 = status_json["sistemas"][1]
        self.assertEqual(s2["id"], "raiznet-site")
        self.assertEqual(s2["status"], "fora")
        self.assertEqual(len(s2["quedas_recentes"]), 1)

        # Sanitização: nenhum ARN, segredo ou IP interno
        json_texto = json.dumps(status_json)
        self.assertNotIn("arn:aws:", json_texto)
        self.assertNotIn("AWS_", json_texto)
        self.assertNotIn("Secret", json_texto)

    def test_limite_baldes_24h(self):
        """Garante que a lista de baldes de 5 min nunca ultrapassa 288 elementos."""
        agora = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        baldes = []

        # Adiciona 400 baldes sucessivos de 5 minutos (> 24 horas)
        for i in range(400):
            ts = (agora - timedelta(minutes=5 * (400 - i))).isoformat()
            baldes = atualizar_baldes_24h(baldes, ts, ok=True, latencia_ms=30)

        self.assertLessEqual(len(baldes), 288)

    def test_limite_historico_dias_90d(self):
        """Garante que o histórico diário expira dados com mais de 90 dias."""
        agora = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)
        historico = {}

        # Simula 120 dias
        for d in range(120):
            dia = (agora - timedelta(days=(120 - d))).isoformat()
            historico = atualizar_historico_dias(historico, dia, ok=True)

        self.assertLessEqual(len(historico), 91)
        # O dia de 115 dias atrás não deve estar presente
        dia_antigo = (agora - timedelta(days=115)).strftime("%Y-%m-%d")
        self.assertNotIn(dia_antigo, historico)


if __name__ == "__main__":
    unittest.main()
