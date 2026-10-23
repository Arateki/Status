"""Testes de ponta a ponta do handler da AWS Lambda."""

from datetime import datetime, timezone
import json
import unittest
from unittest.mock import MagicMock, patch

from checador.checker import ResultadoChecagem
from checador.evaluator import EstadoSistema
from checador.lambda_handler import handler
from checador.storage import (
    ErroGravacaoDynamoDB,
    ErroLeituraDynamoDB,
    ErroPublicacaoR2,
)


class TestLambdaHandler(unittest.TestCase):
    @patch("checador.lambda_handler.publicar_status_json")
    @patch("checador.lambda_handler.salvar_estado_e_checagem")
    @patch("checador.lambda_handler.enviar_alerta_sns")
    @patch("checador.lambda_handler.checar_todos")
    @patch("checador.lambda_handler.carregar_baldes_24h")
    @patch("checador.lambda_handler.carregar_estado_sistema")
    def test_handler_fluxo_completo(
        self,
        mock_carregar_estado,
        mock_carregar_baldes,
        mock_checar_todos,
        mock_enviar_sns,
        mock_salvar_estado,
        mock_publicar_json,
    ):
        """Executa handler com mocks e valida respostas e chamadas de persistência."""
        mock_carregar_estado.return_value = EstadoSistema(sistema_id="arateki-site", status="no_ar")
        mock_carregar_baldes.return_value = [{"inicio": "2026-10-23T14:55:00Z", "latencia_ms": 25, "ok": True}]
        mock_checar_todos.return_value = [
            ResultadoChecagem(
                sistema_id="arateki-site",
                nome="Site da Arateki",
                url="https://arateki.com",
                timestamp="2026-10-23T15:00:00Z",
                ok=True,
                codigo_http=200,
                latencia_ms=25,
                erro=None,
            )
        ]
        mock_salvar_estado.return_value = True
        mock_publicar_json.return_value = True

        evento = {}
        contexto = MagicMock()

        resposta = handler(evento, contexto)

        self.assertEqual(resposta["statusCode"], 200)
        corpo = json.loads(resposta["body"])
        self.assertEqual(corpo["status_geral"], "operacional")
        self.assertEqual(corpo["sistemas_checados"], 1)

        mock_salvar_estado.assert_called_once()
        mock_publicar_json.assert_called_once()
        mock_enviar_sns.assert_not_called()

    @patch("checador.lambda_handler.publicar_status_json")
    @patch("checador.lambda_handler.salvar_estado_e_checagem")
    @patch("checador.lambda_handler.checar_todos")
    @patch("checador.lambda_handler.carregar_estado_sistema")
    def test_handler_aborta_se_leitura_dynamodb_falhar(
        self,
        mock_carregar_estado,
        mock_checar_todos,
        mock_salvar_estado,
        mock_publicar_json,
    ):
        """Se o DynamoDB falhar na leitura, o handler lança exceção para visibilidade no CloudWatch."""
        mock_carregar_estado.side_effect = ErroLeituraDynamoDB("Falha no get_item")

        evento = {}
        contexto = MagicMock()

        with self.assertRaises(ErroLeituraDynamoDB):
            handler(evento, contexto)

        mock_checar_todos.assert_not_called()
        mock_salvar_estado.assert_not_called()
        mock_publicar_json.assert_not_called()

    @patch("checador.lambda_handler.publicar_status_json")
    @patch("checador.lambda_handler.confirmar_alerta_enviado")
    @patch("checador.lambda_handler.enviar_alerta_sns")
    @patch("checador.lambda_handler.salvar_estado_e_checagem")
    @patch("checador.lambda_handler.checar_todos")
    @patch("checador.lambda_handler.carregar_baldes_24h")
    @patch("checador.lambda_handler.carregar_estado_sistema")
    def test_handler_falha_ao_salvar_dynamodb_aborta_sem_disparar_sns(
        self,
        mock_carregar_estado,
        mock_carregar_baldes,
        mock_checar_todos,
        mock_salvar_estado,
        mock_enviar_sns,
        mock_confirmar_alerta,
        mock_publicar_json,
    ):
        """Se a gravação no DynamoDB falhar, o handler lança exceção e NÃO dispara SNS (sem duplicatas)."""
        # Sistema que estava com 1 falha e agora terá a 2ª (queda)
        est_ant = EstadoSistema(sistema_id="arateki-site", status="no_ar", falhas_consecutivas=1, versao=1)
        mock_carregar_estado.return_value = est_ant
        mock_carregar_baldes.return_value = []
        mock_checar_todos.return_value = [
            ResultadoChecagem(
                sistema_id="arateki-site",
                nome="Site da Arateki",
                url="https://arateki.com",
                timestamp="2026-10-23T15:00:00Z",
                ok=False,
                codigo_http=500,
                latencia_ms=100,
                erro="HTTP 500",
            )
        ]
        # Falha na persistência no DynamoDB
        mock_salvar_estado.side_effect = ErroGravacaoDynamoDB("Falha condicional ou timeout")

        with self.assertRaises(ErroGravacaoDynamoDB):
            handler({}, MagicMock())

        # SNS NÃO pode ser disparado se a persistência do novo estado falhou
        mock_enviar_sns.assert_not_called()
        mock_confirmar_alerta.assert_not_called()
        mock_publicar_json.assert_not_called()

    @patch("checador.lambda_handler.publicar_status_json")
    @patch("checador.lambda_handler.confirmar_alerta_enviado")
    @patch("checador.lambda_handler.enviar_alerta_sns")
    @patch("checador.lambda_handler.salvar_estado_e_checagem")
    @patch("checador.lambda_handler.checar_todos")
    @patch("checador.lambda_handler.carregar_baldes_24h")
    @patch("checador.lambda_handler.carregar_estado_sistema")
    def test_handler_sns_falha_alerta_fica_pendente_sem_confirmar(
        self,
        mock_carregar_estado,
        mock_carregar_baldes,
        mock_checar_todos,
        mock_salvar_estado,
        mock_enviar_sns,
        mock_confirmar_alerta,
        mock_publicar_json,
    ):
        """Se o envio SNS falhar, o alerta fica pendente e confirmar_alerta_enviado NÃO é chamado."""
        est_ant = EstadoSistema(sistema_id="arateki-site", status="no_ar", falhas_consecutivas=1, versao=2)
        mock_carregar_estado.return_value = est_ant
        mock_carregar_baldes.return_value = []
        mock_checar_todos.return_value = [
            ResultadoChecagem(
                sistema_id="arateki-site",
                nome="Site da Arateki",
                url="https://arateki.com",
                timestamp="2026-10-23T15:00:00Z",
                ok=False,
                codigo_http=500,
                latencia_ms=100,
                erro="HTTP 500",
            )
        ]
        mock_salvar_estado.return_value = True
        mock_enviar_sns.return_value = False  # Falha no SNS
        mock_publicar_json.return_value = True

        resp = handler({}, MagicMock())

        self.assertEqual(resp["statusCode"], 200)
        mock_enviar_sns.assert_called_once()
        # Não confirma a entrega pois o SNS falhou
        mock_confirmar_alerta.assert_not_called()

    @patch("checador.lambda_handler.publicar_status_json")
    @patch("checador.lambda_handler.salvar_estado_e_checagem")
    @patch("checador.lambda_handler.checar_todos")
    @patch("checador.lambda_handler.carregar_baldes_24h")
    @patch("checador.lambda_handler.carregar_estado_sistema")
    def test_handler_falha_publicar_r2_lanca_excecao(
        self,
        mock_carregar_estado,
        mock_carregar_baldes,
        mock_checar_todos,
        mock_salvar_estado,
        mock_publicar_json,
    ):
        """Se o upload do status.json falhar, a invocação falha visivelmente."""
        mock_carregar_estado.return_value = EstadoSistema(sistema_id="arateki-site", status="no_ar")
        mock_carregar_baldes.return_value = []
        mock_checar_todos.return_value = [
            ResultadoChecagem(
                sistema_id="arateki-site",
                nome="Site da Arateki",
                url="https://arateki.com",
                timestamp="2026-10-23T15:00:00Z",
                ok=True,
                codigo_http=200,
                latencia_ms=30,
                erro=None,
            )
        ]
        mock_salvar_estado.return_value = True
        mock_publicar_json.side_effect = ErroPublicacaoR2("Falha no upload R2")

        with self.assertRaises(ErroPublicacaoR2):
            handler({}, MagicMock())


if __name__ == "__main__":
    unittest.main()
