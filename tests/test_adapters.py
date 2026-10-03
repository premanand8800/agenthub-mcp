import json
import os
import tempfile
import unittest
from unittest import mock

from agenthub.adapters import Registry, RunSpec
from agenthub.adapters.aider import AiderAdapter
from agenthub.adapters.antigravity import AntigravityAdapter
from agenthub.adapters.claude import ClaudeAdapter
from agenthub.adapters.codex import CodexAdapter
from agenthub.adapters.generic import GenericAdapter, SpecError
from agenthub.adapters.goose import GooseAdapter

DANGER = {"--dangerously-bypass-approvals-and-sandbox", "--dangerously-skip-permissions"}


def build(adapter, prompt="do it", mode="workspace-write", **kw):
    kw.setdefault("workdir", "/w")
    with mock.patch.object(type(adapter), "executable", return_value="/bin/" + adapter.binary):
        return adapter.build_command(RunSpec(prompt=prompt, mode=mode, **kw))


class CodexTests(unittest.TestCase):
    def test_sandboxed_by_default(self):
        argv = build(CodexAdapter()).argv
        self.assertFalse(DANGER & set(argv))
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertIn('approval_policy="never"', argv)

    def test_read_only(self):
        argv = build(CodexAdapter(), mode="read-only").argv
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")

    def test_full(self):
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", build(CodexAdapter(), mode="full").argv)

    def test_prompt_after_double_dash(self):
        argv = build(CodexAdapter(), prompt="--help").argv
        self.assertEqual(argv[-2:], ["--", "--help"])

    def test_resume(self):
        argv = build(CodexAdapter(), session_id="abc-123").argv
        self.assertEqual(argv[1:3], ["exec", "resume"])
        self.assertEqual(argv[-3:], ["--", "abc-123", "do it"])
        self.assertIn('sandbox_mode="workspace-write"', argv)


class AntigravityTests(unittest.TestCase):
    def test_modes(self):
        a = AntigravityAdapter()
        self.assertFalse(DANGER & set(build(a).argv))
        self.assertIn("--sandbox", build(a).argv)
        self.assertIn("plan", build(a, mode="read-only").argv)
        self.assertIn("--dangerously-skip-permissions", build(a, mode="full").argv)

    def test_prompt_is_flag_value(self):
        self.assertEqual(build(AntigravityAdapter(), prompt="-x").argv[-1], "--print=-x")


class OtherAdapterTests(unittest.TestCase):
    def test_aider(self):
        argv = build(AiderAdapter(), mode="read-only").argv
        self.assertIn("--dry-run", argv)
        self.assertEqual(argv[-1], "--message=do it")

    def test_goose_modes(self):
        self.assertEqual(GooseAdapter.modes, ("read-only", "full"))
        self.assertEqual(build(GooseAdapter(), mode="read-only").env["GOOSE_MODE"], "chat")


class GenericSpecTests(unittest.TestCase):
    base = {"name": "x", "command": "x", "modes": {"workspace-write": []}}

    def test_validation(self):
        GenericAdapter(self.base)
        for bad in (
            {**self.base, "name": "Bad Name"},
            {**self.base, "modes": {}},
            {**self.base, "modes": {"root": []}},
            {**self.base, "args": ["no placeholder"]},
            {k: v for k, v in self.base.items() if k != "command"},
        ):
            with self.assertRaises(SpecError):
                GenericAdapter(bad)

    def test_placeholders_expand_once(self):
        a = GenericAdapter({**self.base, "args": ["--p={prompt}"], "model_args": ["--m", "{model}"]})
        argv = build(a, prompt="{model}", model="m1").argv
        self.assertEqual(argv[1:], ["--m", "m1", "--p={model}"])


class RegistryTests(unittest.TestCase):
    def test_unsafe_or_conflicting_specs_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:

            def write(name, spec, mode):
                p = os.path.join(d, name)
                with open(p, "w") as f:
                    json.dump(spec, f)
                os.chmod(p, mode)

            write("ok.json", {"name": "ok", "command": "ok", "modes": {"read-only": []}}, 0o600)
            write("loose.json", {"name": "loose", "command": "x", "modes": {"read-only": []}}, 0o666)
            write("codex.json", {"name": "codex", "command": "x", "modes": {"read-only": []}}, 0o600)
            reg = Registry(custom_dir=d)
            self.assertIn("ok", reg.names())
            self.assertNotIn("loose", reg.names())
            self.assertIsInstance(reg.get("codex"), CodexAdapter)
            self.assertEqual(len(reg.load_errors), 2)


CODEX_EVENTS = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "thread.started", "thread_id": "01a1-thread"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py", "kind": "update"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]
)


class CodexJsonTests(unittest.TestCase):
    def test_parse_events(self):
        p = CodexAdapter().parse_output("Reading additional input from stdin...\n" + CODEX_EVENTS)
        self.assertEqual(p.session_id, "01a1-thread")
        self.assertEqual(p.reply, "Done.")
        self.assertEqual(p.usage["output_tokens"], 2)
        self.assertIsNone(p.error)

    def test_readable_log(self):
        log = CodexAdapter().format_log(CODEX_EVENTS)
        self.assertIn("$ ls", log)
        self.assertIn("[edit] a.py", log)
        self.assertIn("[assistant] Done.", log)
        self.assertNotIn("{", log.split("[usage]")[0])

    def test_new_inputs(self):
        argv = build(CodexAdapter(), add_dirs=["/x"], images=["/i.png"], schema_file="/s.json").argv
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], "/x")
        self.assertEqual(argv[argv.index("-i") + 1], "/i.png")
        self.assertEqual(argv[argv.index("--output-schema") + 1], "/s.json")
        self.assertEqual(argv[-2:], ["--", "do it"])


class ClaudeTests(unittest.TestCase):
    def test_modes_and_isolation_from_mcp(self):
        a = ClaudeAdapter()
        argv = build(a).argv
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(build(a, mode="read-only").argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[-2:], ["--", "do it"])
        self.assertIn("--resume", build(a, session_id="s1").argv)

    def test_parse(self):
        out = json.dumps(
            {
                "type": "result",
                "result": "OK",
                "session_id": "s-1",
                "total_cost_usd": 0.05,
                "is_error": False,
                "usage": {"output_tokens": 3},
            }
        )
        p = ClaudeAdapter().parse_output(out)
        self.assertEqual((p.reply, p.session_id, p.cost_usd), ("OK", "s-1", 0.05))
        self.assertIsNone(p.error)
        self.assertEqual(ClaudeAdapter().parse_output(json.dumps({"is_error": True, "result": "bad"})).error, "bad")


class HealthTests(unittest.TestCase):
    def test_detect_quota(self):
        from agenthub.health import detect_quota

        msg, retry = detect_quota("x\nRESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 137h44m55s.")
        self.assertIn("quota", msg)
        self.assertEqual(retry, 137 * 3600 + 44 * 60 + 55)
        self.assertEqual(detect_quota("Rate limit exceeded, try again in 30s")[1], 30)
        self.assertIsNone(detect_quota("error: file not found")[1] if detect_quota("error: file not found") else None)
        self.assertIsNone(detect_quota("TypeError: undefined is not a function"))
