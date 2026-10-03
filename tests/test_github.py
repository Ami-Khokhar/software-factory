"""factory_mcp.github against a local HTTP server: auth header, Link pagination, files, labels,
refs, PR bodies, errors."""
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
        elif self.path.startswith("/search/issues?"):
            page = 2 if "page=2" in self.path else 1
            item = {"number": page, "repository_url": f"https://api.github.com/repos/o/r{page}"}
            body, link = ({"total_count": 2, "items": [item]},
                          f'<{base}/search/issues?q=x&page=2>; rel="next"' if page == 1 else "")
        elif self.path == "/repos/o/r/contents/.github/pr-gatekeeper.json":
            text = base64.encodebytes(b'{"verify": "make test"}').decode()  # GitHub wraps it in lines
            body, link = {"type": "file", "encoding": "base64", "content": text}, ""
        elif self.path.startswith("/repos/o/r/actions/runs?"):
            body, link = {"total_count": 2, "workflow_runs": [{"name": "pr-gatekeeper", "id": 2},
                                                              {"name": "ci", "id": 1}]}, ""
        elif self.path.startswith("/repos/o/r/pulls?"):
            body, link = [{"number": 3}], ""
        elif self.path in ("/repos/o/r/issues/404", "/repos/o/r/contents/.github/missing.json"):
            body, link, status = {"message": "Not Found"}, "", 404
        elif self.path == "/repos/o/r/git/matching-refs/":
            body, link = [{"ref": "refs/tags/v1"}], ""
        elif self.path == "/repos/o/empty/git/matching-refs/":
            body, link, status = {"message": "Git Repository is empty."}, "", 409
        else:
            body, link = {"number": 1}, ""
        self.reply(status, body, link)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        Api.seen.append((self.path, body))
        if self.path == "/repos/o/r/labels" and body["name"] == "factory:ready":
            self.reply(422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]})
        elif self.path == "/repos/o/r/labels" and body["color"] == "not-a-color":
            self.reply(422, {"message": "Validation Failed", "errors": [{"code": "invalid"}]})
        elif self.path == "/repos/o/r/labels":
            self.reply(201, body)
        else:
            self.reply(200, [{"name": name} for name in body["labels"]])

    def do_PATCH(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        Api.seen.append((self.path, body))
        self.reply(200, dict(body, number=int(self.path.rsplit("/", 1)[-1])))

    def reply(self, status, body, link=""):
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

    def test_issues_by_an_author_and_label_come_from_every_repo_with_their_repo_named(self):
        found = self.github.labelled_issues_by("amteshwar", "factory:ready")
        self.assertEqual([(i["repo"], i["number"]) for i in found], [("o/r1", 1), ("o/r2", 2)])
        self.assertEqual(Api.seen[0][0], "/search/issues?q=is%3Aissue+is%3Aopen+label%3A%22factory%3Aready%22"
                                         "+author%3Aamteshwar&per_page=100")

    def test_a_file_is_its_decoded_text_or_none(self):
        self.assertEqual(self.github.file("o/r", ".github/pr-gatekeeper.json"), '{"verify": "make test"}')
        self.assertIsNone(self.github.file("o/r", ".github/missing.json"))

    def test_open_pulls_asks_for_the_open_prs_into_a_base(self):
        self.assertEqual(self.github.open_pulls("o/r", "main"), [{"number": 3}])
        self.assertEqual(Api.seen[0][0], "/repos/o/r/pulls?state=open&base=main&per_page=100")

    def test_a_label_that_exists_already_is_not_an_error(self):
        self.github.create_label("o/r", "factory:ready", "0E8A16", "go")
        self.github.create_label("o/r", "factory:proposed", "C5DEF5", "maybe")
        self.assertEqual(Api.seen[-1], ("/repos/o/r/labels", {"name": "factory:proposed", "color": "C5DEF5",
                                                             "description": "maybe"}))
        with self.assertRaisesRegex(GitHubError, "invalid"):
            self.github.create_label("o/r", "factory:x", "not-a-color", "x")
        self.assertEqual(self.github.add_labels("o/r", 7, ["factory:ready"]), [{"name": "factory:ready"}])
        self.assertEqual(Api.seen[-1], ("/repos/o/r/issues/7/labels", {"labels": ["factory:ready"]}))

    def test_a_repo_has_refs_unless_github_says_it_is_empty(self):
        self.assertTrue(self.github.has_refs("o/r"))
        self.assertFalse(self.github.has_refs("o/empty"))
        self.assertEqual(Api.seen[0][0], "/repos/o/r/git/matching-refs/")

    def test_workflow_runs_are_the_named_workflows_runs_on_a_commit(self):
        self.assertEqual(self.github.workflow_runs("o/r", "abc"), [{"name": "pr-gatekeeper", "id": 2}])
        self.assertEqual(Api.seen[0][0], "/repos/o/r/actions/runs?head_sha=abc&per_page=100")

    def test_one_pull_request_is_read_by_its_number(self):
        self.assertEqual(self.github.pull("o/r", 7), {"number": 1})
        self.assertEqual(Api.seen[0][0], "/repos/o/r/pulls/7")

    def test_one_commit_is_read_by_its_sha(self):
        self.assertEqual(self.github.commit("o/r", "abc"), {"number": 1})
        self.assertEqual(Api.seen[0][0], "/repos/o/r/commits/abc")

    def test_editing_a_pull_request_replaces_its_body(self):
        self.assertEqual(self.github.edit_pull("o/r", 7, "new body"), {"number": 7, "body": "new body"})
        self.assertEqual(Api.seen, [("/repos/o/r/pulls/7", {"body": "new body"})])

    def test_errors_carry_status(self):
        with self.assertRaises(GitHubError) as caught:
            self.github.issue("o/r", 404)
        self.assertEqual(caught.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
