import json
import os
import shutil
import subprocess
import tempfile
import unittest

from agenthub.config import load_config
from agenthub.hub import Hub

FAKE_AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_agent.py")


class HubTestCase(unittest.TestCase):
    """A Hub with a temp AGENTHUB_HOME, a temp trusted workspace and a fake agent named 'fake'."""

    config_overrides: dict = {}

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="agenthub-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.work = os.path.join(self.tmp, "work")
        self.secret = os.path.join(self.work, "secret")
        os.makedirs(os.path.join(self.home, "agents"))
        os.makedirs(self.secret)
        os.chmod(FAKE_AGENT, 0o755)
        cfg = {"trusted_workspaces": [self.work], "denied_paths": [self.secret], **self.config_overrides}
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump(cfg, f)
        fake = {
            "name": "fake",
            "command": FAKE_AGENT,
            "args": ["{prompt}"],
            "model_args": ["--model={model}"],
            "output_schema_args": ["--schema", "{schema_file}"],
            "add_dir_args": ["--add-dir", "{dir}"],
            "env_passthrough": ["FAKE_AGENT_KEY"],
            "modes": {"read-only": ["ro"], "workspace-write": ["ww"], "full": ["full"]},
        }
        # Second agent: answers in JSON with a session ID, like `claude -p --output-format json`.
        fake2 = {
            "name": "fake2",
            "command": FAKE_AGENT,
            "args": ["{prompt}"],
            "json_output": {"reply": "result", "session_id": "session_id", "cost_usd": "cost"},
            "modes": {"read-only": ["ro", "--json-out"], "workspace-write": ["ww", "--json-out"]},
        }
        for spec in (fake, fake2):
            spec_path = os.path.join(self.home, "agents", spec["name"] + ".json")
            with open(spec_path, "w") as f:
                json.dump(spec, f)
            os.chmod(spec_path, 0o600)
        old = os.environ.get("AGENTHUB_DEPTH")
        os.environ.pop("AGENTHUB_DEPTH", None)
        self.addCleanup(
            lambda: (
                os.environ.__setitem__("AGENTHUB_DEPTH", old)
                if old is not None
                else os.environ.pop("AGENTHUB_DEPTH", None)
            )
        )
        self.hub = Hub(load_config(self.home))
        self.addCleanup(self.hub.shutdown)

    def git_repo(self, path=None):
        """Turn the workspace into a git repo with one commit; return its path."""
        path = path or self.work
        for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
            subprocess.run(["git", *cmd], cwd=path, check=True)
        with open(os.path.join(path, "app.py"), "w") as f:
            f.write("print('v1')\n")
        subprocess.run(["git", "add", "app.py"], cwd=path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)
        return path
