"""Testes unitários para o checador HTTP com servidor local falso, paralelismo e timeout."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

from checador.checker import checar_sistema, checar_todos
from checador.config import USER_AGENT


class FakeHTTPHandler(BaseHTTPRequestHandler):
    """Handler HTTP simulado para testes unitários locais."""

    ultimos_headers_recebidos: dict[str, str] = {}

    def log_message(self, format, *args):
        # Silencia logs de requisição no terminal durante os testes
        pass

    def do_GET(self):
        try:
            FakeHTTPHandler.ultimos_headers_recebidos = {k: v for k, v in self.headers.items()}

            if self.path == "/ok":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK da Arateki")

            elif self.path == "/health-ok":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

            elif self.path == "/health-errado":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "error"}')

            elif self.path == "/erro-500":
                self.send_response(500)
                self.end_headers()
                self.wfile.write(b"Internal Error")

            elif self.path == "/cf-403":
                self.send_response(403)
                self.send_header("Server", "cloudflare")
                self.send_header("cf-ray", "938201fa9102-GRU")
                self.end_headers()
                self.wfile.write(b"error code: 1020 Ray ID: 938201fa9102-GRU Cloudflare")

            elif self.path.startswith("/delay"):
                # Simula lentidão
                time.sleep(0.3)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"demorado")

            else:
                self.send_response(404)
                self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass


class TestChecker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Inicia servidor HTTP multithreaded local em porta efêmera (0)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHTTPHandler)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_checagem_sucesso_e_user_agent(self):
        """Checagem simples com 200 OK e conferência do cabeçalho User-Agent."""
        sistema = {
            "id": "teste-ok",
            "nome": "Sistema OK",
            "url": f"{self.base_url}/ok",
            "espera_codigo": 200,
        }
        res = checar_sistema(sistema, timeout=2)

        self.assertTrue(res.ok)
        self.assertEqual(res.codigo_http, 200)
        self.assertIsNotNone(res.latencia_ms)
        self.assertGreaterEqual(res.latencia_ms, 0)
        self.assertIsNone(res.erro)
        self.assertFalse(res.erro_checador)

        # Confere se o User-Agent próprio foi enviado corretamente
        header_ua = FakeHTTPHandler.ultimos_headers_recebidos.get("User-Agent")
        self.assertEqual(header_ua, USER_AGENT)

    def test_checagem_espera_corpo(self):
        """Valida que checagem de corpo esperado funciona tanto no sucesso quanto na falha."""
        sistema_ok = {
            "id": "api-ok",
            "url": f"{self.base_url}/health-ok",
            "espera_codigo": 200,
            "espera_corpo": "ok",
        }
        res_ok = checar_sistema(sistema_ok, timeout=2)
        self.assertTrue(res_ok.ok)

        sistema_errado = {
            "id": "api-errado",
            "url": f"{self.base_url}/health-errado",
            "espera_codigo": 200,
            "espera_corpo": "ok",
        }
        res_errado = checar_sistema(sistema_errado, timeout=2)
        self.assertFalse(res_errado.ok)
        self.assertIn("Corpo não contém 'ok'", res_errado.erro or "")

    def test_checagem_erro_http(self):
        """Respostas de erro HTTP (ex: 500) marcam falha e registram o código."""
        sistema = {
            "id": "sistema-erro",
            "url": f"{self.base_url}/erro-500",
            "espera_codigo": 200,
        }
        res = checar_sistema(sistema, timeout=2)
        self.assertFalse(res.ok)
        self.assertEqual(res.codigo_http, 500)
        self.assertIn("500", res.erro or "")
        self.assertFalse(res.erro_checador)

    def test_deteccao_403_cloudflare_como_erro_checador(self):
        """Identifica 403 proveniente de bloqueio do Cloudflare como erro do checador."""
        sistema = {
            "id": "sistema-cf",
            "url": f"{self.base_url}/cf-403",
            "espera_codigo": 200,
        }
        res = checar_sistema(sistema, timeout=2)
        self.assertFalse(res.ok)
        self.assertEqual(res.codigo_http, 403)
        self.assertTrue(res.erro_checador, "Deve identificar bloqueio do Cloudflare como erro_checador")
        self.assertIn("Cloudflare 403", res.erro or "")

    def test_timeout_por_sistema(self):
        """Garante que requisição acima do timeout dispara erro de Timeout."""
        sistema = {
            "id": "sistema-lento",
            "url": f"{self.base_url}/delay",
            "espera_codigo": 200,
        }
        # Timeout de 0.1s contra endpoint que demora 0.3s
        inicio = time.perf_counter()
        res = checar_sistema(sistema, timeout=1)  # testamos com timeout baixo usando urllib
        # Vamos testar timeout real passando timeout=0.08
        res_timeout = checar_sistema(sistema, timeout=0.08)
        duracao = time.perf_counter() - inicio

        self.assertFalse(res_timeout.ok)
        self.assertIsNone(res_timeout.codigo_http)
        self.assertIn("Timeout", res_timeout.erro or "")

    def test_checagem_em_paralelo(self):
        """Garante que a checagem de múltiplos sistemas roda em paralelo e não sequencialmente."""
        # 3 sistemas que demoram 0.3s cada. Se sequencial, demoraria >= 0.9s.
        sistemas = [
            {"id": "sis1", "url": f"{self.base_url}/delay"},
            {"id": "sis2", "url": f"{self.base_url}/delay"},
            {"id": "sis3", "url": f"{self.base_url}/delay"},
        ]

        inicio = time.perf_counter()
        resultados = checar_todos(sistemas, timeout=2)
        duracao_total = time.perf_counter() - inicio

        self.assertEqual(len(resultados), 3)
        for r in resultados:
            self.assertTrue(r.ok)

        # Paralelo com 3 threads deve durar bem menos que 3 * 0.3s (0.9s)
        self.assertLess(duracao_total, 0.7, f"Checagens deveriam rodar em paralelo; durou {duracao_total:.2f}s")


if __name__ == "__main__":
    unittest.main()
