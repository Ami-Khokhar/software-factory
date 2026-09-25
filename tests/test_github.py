"""factory_mcp.github against a local HTTP server: auth header, Link pagination, files, errors."""
import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from factory_mcp.github import GitHub, GitHubError


class Api(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        Api.seen.append((self.path, self.headers.get("Authorization")))
        base, status = f"http://127.0.0.1:{self.server.server_port}", 200
        if self.path.startswith("/repos/o/r/issues/5/comments"):
            page = 2 if "page=2" in self.path else 1
            body, link = ([{"id": page}], f'<{base}/repos/o/r/issues/5/comments?page=2>; rel="next"'
                          if page == 1 else "")
        elif self.path == "/repos/o/r/contents/.github/pr-gatekeeper.json":
            text = base64.encodebytes(b'{"verify": "make test"}').decode()  # GitHub wraps it in lines
            body, link = {"type": "file", "encoding": "base64", "content": text}, ""
        elif self.path in ("/repos/o/r/issues/404", "/repos/o/r/contents/.github/missing.json"):
            body, link, status = {"message": "Not Found"}, "", 404
        else:
            body, link = {"number": 1}, ""
        self.send_response(status)
        data = json.dumps(body).encode()
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        if link:
            self.send_header("Link", link)
        self.end_headers()
        self.wfile.write(data)


class GitHubClientTest(unittest.TestCase):
    def setUp(self):
        Api.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.github = GitHub("tok", api=f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_paginates_and_authenticates(self):
        self.assertEqual(self.github.comments("o/r", 5), [{"id": 1}, {"id": 2}])
        self.assertEqual(len(Api.seen), 2)
        self.assertTrue(all(auth == "Bearer tok" for _, auth in Api.seen))
        self.assertIn("per_page=100", Api.seen[0][0])

    def test_a_file_is_its_decoded_text_or_none(self):
        self.assertEqual(self.github.file("o/r", ".github/pr-gatekeeper.json"), '{"verify": "make test"}')
        self.assertIsNone(self.github.file("o/r", ".github/missing.json"))

    def test_errors_carry_status(self):
        with self.assertRaises(GitHubError) as caught:
            self.github.issue("o/r", 404)
        self.assertEqual(caught.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
