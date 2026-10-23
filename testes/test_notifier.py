"""Testes unitários para o módulo de notificações via Amazon SNS."""

import unittest
from unittest.mock import MagicMock

from checador.notifier import enviar_alerta_sns


class TestNotifier(unittest.TestCase):
    def test_alerta_queda_sns(self):
        """Verifica formatação da mensagem e assunto na queda."""
        mock_sns = MagicMock()
        dados = {
            "sistema_id": "arateki-site",
            "nome": "Site da Arateki",
            "url": "https://arateki.com",
            "inicio": "2026-10-23T14:00:00Z",
            "motivo": "HTTP 500",
            "codigo_http": 500,
        }

        sucesso = enviar_alerta_sns(
            "notificar_queda",
            dados,
            topic_arn="arn:aws:sns:us-east-2:123456789012:arateki-status-alertas",
            sns_client=mock_sns,
        )

        self.assertTrue(sucesso)
        mock_sns.publish.assert_called_once()
        _, kwargs = mock_sns.publish.call_args
        self.assertIn("FORA DO AR", kwargs["Subject"])
        self.assertIn("Site da Arateki", kwargs["Subject"])
        self.assertIn("HTTP 500", kwargs["Message"])
        self.assertIn("https://arateki.com", kwargs["Message"])

    def test_alerta_volta_sns(self):
        """Verifica formatação da mensagem e assunto na volta."""
        mock_sns = MagicMock()
        dados = {
            "sistema_id": "keyflow-api",
            "nome": "API do KeyFlow",
            "url": "https://keyflow-api.arateki.com/health",
            "inicio": "2026-10-23T14:00:00Z",
            "fim": "2026-10-23T14:15:00Z",
            "duracao_texto": "15m",
        }

        sucesso = enviar_alerta_sns(
            "notificar_volta",
            dados,
            topic_arn="arn:aws:sns:us-east-2:123456789012:arateki-status-alertas",
            sns_client=mock_sns,
        )

        self.assertTrue(sucesso)
        mock_sns.publish.assert_called_once()
        _, kwargs = mock_sns.publish.call_args
        self.assertIn("voltou ao ar", kwargs["Subject"])
        self.assertIn("15m", kwargs["Message"])

    def test_ignora_sem_topic_arn(self):
        """Retorna False e não falha se topic_arn estiver vazio."""
        sucesso = enviar_alerta_sns("notificar_queda", {}, topic_arn="")
        self.assertFalse(sucesso)


if __name__ == "__main__":
    unittest.main()
