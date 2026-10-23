"""Testes de ponta a ponta do handler da AWS Lambda."""

import json
import unittest
from unittest.mock import MagicMock, patch

from checador.checker import ResultadoChecagem
from checador.evaluator import EstadoSistema
from checador.lambda_handler import handler
from checador.storage import ErroLeituraDynamoDB


class TestLambdaHandler(unittest.TestCase):
    @patch("checador.lambda_handler.publicar_status_json")
    @patch("checador.lambda_handler.salvar_estado_e_checagem")
    @patch("checador.lambda_handler.enviar_alerta_sns")
    @patch("checador.lambda_handler.checar_todos")
    @patch("checador.lambda_handler.carregar_estado_sistema")
    def test_handler_fluxo_completo(
        self,
        mock_carregar_estado,
        mock_checar_todos,
        mock_enviar_sns,
        mock_salvar_estado,
        mock_publicar_json,
    ):
        """Executa handler com mocks e valida respostas e chamadas de persistência."""
        mock_carregar_estado.return_value = EstadoSistema(sistema_id="arateki-site", status="no_ar")
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
        """Se o DynamoDB falhar na leitura, o handler aborta sem salvar nem publicar nada."""
        mock_carregar_estado.side_effect = ErroLeituraDynamoDB("Falha no get_item")

        evento = {}
        contexto = MagicMock()

        resposta = handler(evento, contexto)

        self.assertEqual(resposta["statusCode"], 500)
        corpo = json.loads(resposta["body"])
        self.assertEqual(corpo["status"], "abortado_por_seguranca")

        # Garante que o estado anterior não foi sobrescrito nem o status.json publicado
        mock_checar_todos.assert_not_called()
        mock_salvar_estado.assert_not_called()
        mock_publicar_json.assert_not_called()


if __name__ == "__main__":
    unittest.main()
