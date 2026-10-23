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
    ErroGravacaoDynamoDB,
    ErroLeituraDynamoDB,
    ErroPublicacaoR2,
    carregar_baldes_24h,
    carregar_estado_sistema,
    confirmar_alerta_enviado,
    construir_status_json,
    publicar_status_json,
    salvar_balde_hora,
    salvar_estado_e_checagem,
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
        self.assertIn("dias_90", s1, "Deve incluir agregado diário de 90 dias")
        self.assertEqual(len(s1["dias_90"]), 90)
        self.assertEqual(s1["dias_90"][-1]["data"], "2026-10-23")
        self.assertEqual(s1["dias_90"][-1]["status"], "verde")

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

    def test_salvar_estado_compacto_sem_baldes_24h(self):
        """O item STATE no DynamoDB não deve conter o campo volumoso baldes_24h."""
        mock_client = MagicMock()
        estado = EstadoSistema(
            sistema_id="arateki-site",
            status="no_ar",
            baldes_24h=[{"inicio": "2026-10-23T14:00:00Z", "latencia_ms": 20, "ok": True}],
            versao=1,
        )
        res_check = {"ok": True, "codigo_http": 200, "latencia_ms": 20, "timestamp": "2026-10-23T14:05:00Z"}

        salvar_estado_e_checagem(estado, res_check, versao_esperada=0, dynamo_client=mock_client)

        # Procura a chamada put_item para SK="STATE"
        chamadas_put = mock_client.put_item.call_args_list
        put_state = next(c for c in chamadas_put if c.kwargs["Item"]["SK"]["S"] == "STATE")
        dados_json_salvos = json.loads(put_state.kwargs["Item"]["dados_json"]["S"])

        self.assertNotIn("baldes_24h", dados_json_salvos, "baldes_24h deve ficar fora do item STATE regravado")
        self.assertEqual(put_state.kwargs["ConditionExpression"], "attribute_not_exists(PK)")

    def test_salvar_balde_hora_e_carregar_baldes_24h(self):
        """Verifica a escrita dos baldes da hora corrente e a leitura agrupada das 24h."""
        mock_client = MagicMock()
        agora = datetime(2026, 10, 23, 15, 30, 0, tzinfo=timezone.utc)
        agora_iso = agora.isoformat()

        baldes = [
            {"inicio": "2026-10-23T15:00:00+00:00", "latencia_ms": 25, "ok": True},
            {"inicio": "2026-10-23T15:05:00+00:00", "latencia_ms": 30, "ok": True},
        ]

        salvar_balde_hora("arateki-site", agora_iso, baldes, dynamo_client=mock_client)

        # Confere chamada put_item com SK HOUR#2026-10-23T15
        mock_client.put_item.assert_called_once()
        chamada = mock_client.put_item.call_args.kwargs
        self.assertEqual(chamada["Item"]["SK"]["S"], "HOUR#2026-10-23T15")
        self.assertIn("ttl", chamada["Item"])

        # Agora testa consulta via Query
        mock_client.query.return_value = {
            "Items": [
                {"baldes_json": {"S": json.dumps(baldes)}},
            ]
        }
        baldes_lidos = carregar_baldes_24h("arateki-site", dynamo_client=mock_client, agora_dt=agora)
        self.assertEqual(len(baldes_lidos), 2)
        self.assertEqual(baldes_lidos[0]["inicio"], "2026-10-23T15:00:00+00:00")

    def test_confirmar_alerta_enviado_atualiza_versao_e_limpa_pendencia(self):
        """Confirmação de envio de alerta limpa alerta_pendente, atualiza ultimo_notificado e incrementa versao."""
        mock_client = MagicMock()
        estado = EstadoSistema(
            sistema_id="arateki-site",
            status="fora",
            ultimo_notificado="no_ar",
            alerta_pendente={"acao": "notificar_queda", "dados": {}},
            versao=2,
        )

        confirmar_alerta_enviado(estado, dynamo_client=mock_client)

        self.assertEqual(estado.ultimo_notificado, "fora")
        self.assertIsNone(estado.alerta_pendente)
        self.assertEqual(estado.versao, 3)

        mock_client.put_item.assert_called_once()
        kwargs = mock_client.put_item.call_args.kwargs
        self.assertEqual(kwargs["ConditionExpression"], "attribute_not_exists(PK) OR versao = :v_ant")
        self.assertEqual(kwargs["ExpressionAttributeValues"][":v_ant"]["N"], "2")

    def test_publicar_status_json_lanca_excecao_em_erro(self):
        """Falha no upload do status.json para o bucket lança ErroPublicacaoR2."""
        mock_s3 = MagicMock()
        mock_s3.put_object.side_effect = RuntimeError("Falha de rede R2")

        with self.assertRaises(ErroPublicacaoR2):
            publicar_status_json({"status_geral": "operacional"}, bucket_name="arateki-status", s3_client=mock_s3)

    def test_cloudflare_403_gera_status_indisponivel(self):
        """Se o checador for bloqueado pelo Cloudflare, o status do sistema fica 'indisponivel'."""
        sistemas_cfg = [{"id": "keyflow-api", "nome": "API", "url": "https://api.arateki.com"}]
        estados = {
            "keyflow-api": EstadoSistema(
                sistema_id="keyflow-api",
                status="no_ar",
                verificacao_indisponivel=True,
            )
        }

        doc = construir_status_json(sistemas_cfg, estados)

        self.assertEqual(doc["status_geral"], "instabilidade", "Deve acusar instabilidade se verificação estiver indisponível")
        s = doc["sistemas"][0]
        self.assertEqual(s["status"], "indisponivel")
        self.assertTrue(s["verificacao_indisponivel"])


if __name__ == "__main__":
    unittest.main()


class TestClienteR2(unittest.TestCase):
    def test_cliente_r2_usa_regiao_auto(self):
        import sys
        from unittest import mock
        falso = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"boto3": falso}):
            from checador.storage import obter_cliente_s3
            obter_cliente_s3("https://conta.r2.cloudflarestorage.com", "id", "segredo")
        _, kwargs = falso.client.call_args
        self.assertEqual(kwargs["region_name"], "auto")
        self.assertEqual(kwargs["endpoint_url"], "https://conta.r2.cloudflarestorage.com")
