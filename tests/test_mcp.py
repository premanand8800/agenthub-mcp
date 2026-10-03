import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest

from helpers import HubTestCase

from agenthub.mcp_server import TOOLS, MCPServer

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class CollectingOut(io.StringIO):
    def __init__(self):
        super().__init__()
        self.cv = threading.Condition()

    def write(self, s):
        with self.cv:
            n = super().write(s)
            self.cv.notify_all()
            return n

    def messages(self):
        return [json.loads(line) for line in self.getvalue().splitlines() if line.strip()]

    def wait_for(self, msg_id, timeout=10):
        deadline = time.time() + timeout
        with self.cv:
            while time.time() < deadline:
                for m in self.messages():
                    if m.get("id") == msg_id:
                        return m
                self.cv.wait(0.1)
        raise AssertionError(f"no response for id {msg_id}")


class MCPTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.out = CollectingOut()
        self.server = MCPServer(self.hub, stdin=io.StringIO(), stdout=self.out)

    def send(self, msg_id, method, params=None):
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.server.handle_line(json.dumps(msg))
        return self.out.wait_for(msg_id)

    def call(self, msg_id, name, args):
        res = self.send(msg_id, "tools/call", {"name": name, "arguments": args})["result"]
        return res["isError"], json.loads(res["content"][0]["text"])

    def test_initialize_negotiates_version(self):
        r = self.send(
            1,
            "initialize",
            {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        )["result"]
        self.assertEqual(r["protocolVersion"], "2025-03-26")
        self.assertIn("instructions", r)
        r = self.send(2, "initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(r["protocolVersion"], "2025-06-18")

    def test_ping_and_unknown_method(self):
        self.assertEqual(self.send(1, "ping")["result"], {})
        self.assertEqual(self.send(2, "resources/list")["error"]["code"], -32601)

    def test_notifications_get_no_reply(self):
        self.server.handle_line(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.out.messages(), [])

    def test_parse_error(self):
        self.server.handle_line("{not json")
        self.assertEqual(self.out.messages()[0]["error"]["code"], -32700)

    def test_tools_list_has_annotations(self):
        tools = self.send(1, "tools/list")["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertIn("ask", names)
        self.assertNotIn("approve_tools", names)  # human-only, never exposed to models
        for t in tools:
            self.assertIn("readOnlyHint", t["annotations"])
            self.assertFalse(t["inputSchema"]["additionalProperties"])
        self.assertTrue(next(t for t in tools if t["name"] == "list_tasks")["annotations"]["readOnlyHint"])

    def test_ask_round_trip(self):
        err, data = self.call(1, "ask", {"agent": "fake", "prompt": "hi", "workdir": self.work})
        self.assertFalse(err)
        self.assertEqual(json.loads(data["reply"])["argv"], ["ww", "hi"])

    def test_errors_are_tool_errors_not_crashes(self):
        err, data = self.call(1, "ask", {"agent": "fake", "prompt": "hi", "workdir": "/etc"})
        self.assertTrue(err)
        self.assertEqual(data["error"]["code"], "policy_denied")
        err, data = self.call(2, "ask", {"agent": "fake"})
        self.assertTrue(err)
        self.assertIn("prompt", data["error"]["message"])
        err, data = self.call(3, "ask", {"agent": "fake", "prompt": "x", "sandbox": "off"})
        self.assertTrue(err)
        self.assertIn("Unknown argument", data["error"]["message"])
        err, data = self.call(4, "get_task_logs", {"task_id": "x", "tail_lines": "ten"})
        self.assertTrue(err)
        # Server still answers afterwards.
        self.assertEqual(self.send(5, "ping")["result"], {})

    def test_unknown_tool(self):
        r = self.send(1, "tools/call", {"name": "rm_rf", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_slow_call_does_not_block_others(self):
        self.server.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": "slow",
                    "method": "tools/call",
                    "params": {
                        "name": "ask",
                        "arguments": {"agent": "fake", "prompt": "sleep:3", "workdir": self.work},
                    },
                }
            )
        )
        started = time.time()
        self.send("fast", "tools/call", {"name": "list_agents", "arguments": {}})
        self.assertLess(time.time() - started, 2)
        self.out.wait_for("slow")

    def test_new_tools_listed(self):
        names = {t["name"] for t in self.send(1, "tools/list")["result"]["tools"]}
        for n in ("wait_task", "get_task_diff", "apply_task", "discard_task", "review", "compare"):
            self.assertIn(n, names)

    def test_array_items_validated(self):
        err, data = self.call(1, "compare", {"agents": ["fake", 7], "prompt": "hi"})
        self.assertTrue(err)
        self.assertIn("agents[1]", data["error"]["message"])

    def test_progress_notifications(self):
        import agenthub.mcp_server as m

        old = m.PROGRESS_INTERVAL
        m.PROGRESS_INTERVAL = 0.2
        self.addCleanup(setattr, m, "PROGRESS_INTERVAL", old)
        args = {"agent": "fake", "prompt": "sleep:1", "workdir": self.work}
        self.server.handle_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {"name": "ask", "arguments": args, "_meta": {"progressToken": "p1"}},
                }
            )
        )
        self.out.wait_for(9)
        notes = [x for x in self.out.messages() if x.get("method") == "notifications/progress"]
        self.assertTrue(notes)
        self.assertEqual(notes[0]["params"]["progressToken"], "p1")

    def test_start_wait_round_trip(self):
        err, task = self.call(1, "start_task", {"agent": "fake2", "prompt": "hi", "workdir": self.work})
        self.assertFalse(err, task)
        err, done = self.call(2, "wait_task", {"task_id": task["task_id"], "timeout_seconds": 20})
        self.assertFalse(err)
        self.assertEqual((done["status"], done["session_id"]), ("succeeded", "sess-123"))


class MCPSubprocessTest(HubTestCase):
    def test_stdio_end_to_end(self):
        env = dict(os.environ, AGENTHUB_HOME=self.home, PYTHONPATH=SRC)
        proc = subprocess.Popen(
            [sys.executable, "-m", "agenthub", "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "ask", "arguments": {"agent": "fake", "prompt": "e2e", "workdir": self.work}},
            },
        ]
        out, err = proc.communicate("".join(json.dumps(m) + "\n" for m in msgs), timeout=30)
        replies = {m["id"]: m for m in map(json.loads, out.splitlines())}
        self.assertEqual(set(replies), {1, 2, 3}, err)
        self.assertEqual(len(replies[2]["result"]["tools"]), len(TOOLS))
        self.assertFalse(replies[3]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
