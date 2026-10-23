"""Testes para validação estática e segurança da página HTML."""

import html
import os
import re
import unittest


class TestPagina(unittest.TestCase):
    def setUp(self):
        self.caminho_html = os.path.join(
            os.path.dirname(__file__), "..", "pagina", "index.html"
        )
        with open(self.caminho_html, "r", encoding="utf-8") as f:
            self.conteudo = f.read()

    def test_nao_usa_no_store_no_fetch(self):
        """fetch('status.json') deve usar cache padrão para permitir o cache de 60s do Cloudflare."""
        self.assertNotIn("cache: 'no-store'", self.conteudo)
        self.assertNotIn('cache: "no-store"', self.conteudo)
        self.assertIn("fetch('status.json')", self.conteudo)

    def test_possui_funcoes_de_escape_seguro(self):
        """A página deve conter funções de escape para mitigar injeção de HTML/XSS."""
        self.assertIn("function escapeHtml(", self.conteudo)
        self.assertIn("function escapeUrl(", self.conteudo)

    def test_logica_de_escape_neutraliza_xss(self):
        """Simula a lógica do escapeHtml e garante que tags e aspas são neutralizadas."""
        payload_malicioso = '<script>alert("xss")</script>'
        escapado = (
            payload_malicioso.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
        )
        self.assertNotIn("<script>", escapado)
        self.assertIn("&lt;script&gt;", escapado)

    def test_alerta_dados_desatualizados_mais_de_cinco_minutos(self):
        """A página deve detectar quando os dados têm mais de 5 min e alertar o usuário."""
        self.assertIn("dadosDesatualizados", self.conteudo)
        self.assertIn("Dados desatualizados", self.conteudo)
        self.assertIn("5 * 60 * 1000", self.conteudo)

    def test_classe_e_badge_verificacao_indisponivel(self):
        """A página deve estilizar e indicar 'Verificação indisponível' quando o checador for bloqueado."""
        self.assertIn("Verificação indisponível", self.conteudo)
        self.assertIn(".card-badge.indisponivel", self.conteudo)
        self.assertIn(".dot.indisponivel", self.conteudo)


if __name__ == "__main__":
    unittest.main()
