"""Testes unitários para geração do status.json e rollups de armazenamento."""

from datetime import datetime, timezone, timedelta
import json
import unittest

from checador.evaluator import (
    EstadoSistema,
    atualizar_baldes_24h,
    atualizar_historico_dias,
    calcular_porcentagens,
)
from checador.storage import (
    ErroLeituraDynamoDB,
    carregar_estado_sistema,
    construir_status_json,
)
from unittest.mock import MagicMock


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

    def test_baldes_24h_contagem_individual_de_ok_e_falhas(self):
        """Um erro no balde de 5 min não zera todas as outras checagens do cálculo h24."""
        agora = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)
        baldes = []

        # 5 checagens no mesmo intervalo de 5 minutos: 4 OK e 1 Falha
        for i in range(4):
            ts = (agora + timedelta(seconds=i * 30)).isoformat()
            baldes = atualizar_baldes_24h(baldes, ts, ok=True, latencia_ms=40)

        ts_falha = (agora + timedelta(seconds=150)).isoformat()
        baldes = atualizar_baldes_24h(baldes, ts_falha, ok=False, latencia_ms=None)

        self.assertEqual(len(baldes), 1)
        b = baldes[0]
        self.assertEqual(b["total"], 5)
        self.assertEqual(b["total_ok"], 4)
        self.assertFalse(b["ok"], "O balde como um todo deve marcar que houve falha para o gráfico")

        # No cálculo de porcentagem h24, a taxa deve ser 4/5 = 80.0%
        pcts = calcular_porcentagens({}, baldes, timestamp_referencia=agora.isoformat())
        self.assertEqual(pcts["h24"], 80.0)

    def test_carregar_estado_falha_dynamodb_lanca_excecao(self):
        """Erro na consulta ao DynamoDB lança ErroLeituraDynamoDB para não apagar dados."""
        mock_client = MagicMock()
        mock_client.get_item.side_effect = RuntimeError("Conexão recusada ao DynamoDB")

        with self.assertRaises(ErroLeituraDynamoDB):
            carregar_estado_sistema("arateki-site", table_name="arateki-status", dynamo_client=mock_client)

    def test_carregar_estado_item_inexistente_retorna_none(self):
        """Item que realmente não existe retorna None normalmente."""
        mock_client = MagicMock()
        mock_client.get_item.return_value = {}  # sem Item

        res = carregar_estado_sistema("arateki-site", table_name="arateki-status", dynamo_client=mock_client)
        self.assertIsNone(res)


if __name__ == "__main__":
    unittest.main()
