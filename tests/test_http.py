import http.client
import json
import threading

from helpers import HubTestCase

from agenthub.http_server import AgentHubServer

TOKEN = "t" * 43


class HTTPTests(HubTestCase):
    config_overrides = {"http_allowed_origins": ["http://localhost:3000"]}

    def setUp(self):
        super().setUp()
        self.server = AgentHubServer(("127.0.0.1", 0), self.hub, TOKEN)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def req(self, method, path, body=None, token=TOKEN, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        if body is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = json.loads(r.read() or b"null")
        conn.close()
        return r.status, data, r

    def test_health_is_public_and_minimal(self):
        status, data, _ = self.req("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertNotIn("agents", data)

    def test_auth_required(self):
        self.assertEqual(self.req("GET", "/v1/agents", token=None)[0], 401)
        self.assertEqual(self.req("GET", "/v1/agents", token="wrong")[0], 401)
        self.assertEqual(self.req("GET", "/v1/agents?token=" + TOKEN, token=None)[0], 401)
        self.assertEqual(self.req("GET", "/v1/agents")[0], 200)

    def test_rate_limit_after_failed_auth(self):
        for _ in range(10):
            self.req("GET", "/v1/agents", token="wrong")
        self.assertEqual(self.req("GET", "/v1/agents")[0], 429)

    def test_dns_rebinding_host_rejected(self):
        self.assertEqual(self.req("GET", "/v1/agents", headers={"Host": "evil.example:80"})[0], 421)

    def test_origin_allow_list(self):
        self.assertEqual(self.req("GET", "/v1/agents", headers={"Origin": "https://evil.example"})[0], 403)
        status, _, r = self.req("GET", "/v1/agents", headers={"Origin": "http://localhost:3000"})
        self.assertEqual(status, 200)
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "http://localhost:3000")
        self.assertIsNone(self.req("GET", "/v1/agents")[2].getheader("Access-Control-Allow-Origin"))

    def test_post_requires_json(self):
        status, _, _ = self.req("POST", "/v1/agents/fake/ask", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_ask_and_errors(self):
        status, data, _ = self.req("POST", "/v1/agents/fake/ask", {"prompt": "hi", "workdir": self.work})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, data, _ = self.req("POST", "/v1/agents/fake/ask", {"prompt": "hi", "workdir": "/etc"})
        self.assertEqual(status, 403)
        self.assertEqual(data["error"]["code"], "policy_denied")
        status, _, _ = self.req("POST", "/v1/agents/fake/ask", {"prompt": "hi", "sandbox": "off"})
        self.assertEqual(status, 400)
        self.assertEqual(self.req("GET", "/v1/nope")[0], 404)

    def test_body_limit(self):
        # Announce a 2 MB body but send none: the server must refuse from the headers alone.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/v1/agents/fake/ask", skip_host=True)
        for k, v in {
            "Host": f"127.0.0.1:{self.port}",
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Content-Length": str(2 * 1024 * 1024),
        }.items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        self.assertEqual(r.status, 413)
        self.assertEqual(r.getheader("Connection"), "close")
        conn.close()
