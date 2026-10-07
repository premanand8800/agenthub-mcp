"""v1.1 / v1.2 features: env allowlist, quota + fallback, session IDs, wait, worktrees, review, compare."""

import json
import os
import subprocess
import sys
import threading
import time

from helpers import HubTestCase

from agenthub.config import load_config
from agenthub.errors import InvalidArgument, LimitExceeded, QuotaExhausted, Unavailable
from agenthub.hub import Hub


class EnvTests(HubTestCase):
    def test_secrets_are_not_passed(self):
        os.environ["AWS_SECRET_ACCESS_KEY"] = "nope"
        os.environ["FAKE_AGENT_KEY"] = "yes"
        self.addCleanup(os.environ.pop, "AWS_SECRET_ACCESS_KEY", None)
        self.addCleanup(os.environ.pop, "FAKE_AGENT_KEY", None)
        keys = json.loads(self.hub.ask("fake", "hi", workdir=self.work)["reply"])["env_keys"]
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", keys)
        self.assertIn("FAKE_AGENT_KEY", keys)  # adapter's env_passthrough
        self.assertIn("PATH", keys)

    def test_inherit_env_opt_in(self):
        os.environ["AWS_SECRET_ACCESS_KEY"] = "x"
        self.addCleanup(os.environ.pop, "AWS_SECRET_ACCESS_KEY", None)
        self.hub.config.inherit_env = True
        keys = json.loads(self.hub.ask("fake", "hi", workdir=self.work)["reply"])["env_keys"]
        self.assertIn("AWS_SECRET_ACCESS_KEY", keys)


class QuotaTests(HubTestCase):
    def test_quota_is_detected_remembered_and_skipped(self):
        res = self.hub.ask("fake", "quota", workdir=self.work)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "quota_exhausted")
        self.assertEqual(res["retry_after_seconds"], 3723)
        health = {a["name"]: a["health"] for a in self.hub.list_agents()["agents"]}
        self.assertEqual(health["fake"]["status"], "quota_exhausted")
        with self.assertRaises(QuotaExhausted):
            self.hub.ask("fake", "hi", workdir=self.work)
        # Another process sees the same state.
        other = Hub(load_config(self.home))
        with self.assertRaises(QuotaExhausted):
            other.ask("fake", "hi", workdir=self.work)

    def test_fallback(self):
        self.hub.ask("fake", "quota", workdir=self.work)
        res = self.hub.ask("fake", "hi", workdir=self.work, fallback_agents=["fake2"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["agent"], "fake2")
        self.assertEqual(res["fallback_attempts"][0]["agent"], "fake")

    def test_fallback_after_quota_during_run(self):
        res = self.hub.ask("fake", "quota", workdir=self.work, fallback_agents=["fake2"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["agent"], "fake2")

    def test_no_fallback_on_policy_errors(self):
        from agenthub.errors import PolicyError

        with self.assertRaises(PolicyError):
            self.hub.ask("fake", "hi", workdir="/etc", fallback_agents=["fake2"])


class SessionAndSchemaTests(HubTestCase):
    def test_session_id_and_cost_returned(self):
        res = self.hub.ask("fake2", "hi", workdir=self.work)
        self.assertEqual(res["session_id"], "sess-123")
        self.assertEqual(res["cost_usd"], 0.01)
        self.assertTrue(res["reply"].startswith("json reply"))

    def test_output_schema(self):
        res = self.hub.ask("fake", "hi", workdir=self.work, output_schema={"type": "object"})
        self.assertEqual(res["structured"], {"answer": 42})
        with self.assertRaises(Unavailable):
            self.hub.ask("fake2", "hi", workdir=self.work, output_schema={"type": "object"})
        with self.assertRaises(InvalidArgument):
            self.hub.ask("fake", "hi", workdir=self.work, output_schema="not an object")

    def test_add_dirs_policy(self):
        extra = os.path.join(self.work, "lib")
        os.makedirs(extra)
        argv = json.loads(self.hub.ask("fake", "hi", workdir=self.work, add_dirs=[extra])["reply"])["argv"]
        self.assertEqual(argv[argv.index("--add-dir") + 1], extra)
        from agenthub.errors import PolicyError

        with self.assertRaises(PolicyError):
            self.hub.ask("fake", "hi", workdir=self.work, add_dirs=["/etc"])


class WaitAndLimitTests(HubTestCase):
    def test_wait_task(self):
        t = self.hub.start_task("fake2", "sleep:1", workdir=self.work)
        res = self.hub.wait_task(t["task_id"], 20)
        self.assertTrue(res["finished"])
        self.assertEqual(res["status"], "succeeded")
        self.assertEqual(res["session_id"], "sess-123")
        self.assertTrue(res["final_output"].startswith("json reply"))

    def test_wait_task_timeout(self):
        t = self.hub.start_task("fake", "sleep:30", workdir=self.work)
        self.addCleanup(self.hub.cancel_task, t["task_id"])
        started = time.time()
        res = self.hub.wait_task(t["task_id"], 1)
        self.assertFalse(res["finished"])
        self.assertLess(time.time() - started, 3)

    def test_concurrency_limit_across_processes(self):
        self.hub.tasks.max_concurrent = 1
        t = self.hub.start_task("fake", "sleep:30", workdir=self.work)
        self.addCleanup(self.hub.cancel_task, t["task_id"])
        other = Hub(load_config(self.home))  # e.g. a second Claude Code session
        other.tasks.max_concurrent = 1
        with self.assertRaises(LimitExceeded):
            other.start_task("fake", "hi", workdir=self.work)

    def test_shutdown_kills_inflight_ask(self):
        result = {}
        th = threading.Thread(target=lambda: result.update(self.hub.ask("fake", "sleep:30", workdir=self.work)))
        th.start()
        time.sleep(0.5)
        started = time.time()
        self.hub.shutdown()
        th.join(10)
        self.assertFalse(th.is_alive())
        self.assertLess(time.time() - started, 5)
        self.assertEqual(result["status"], "cancelled")
        with self.assertRaises(Unavailable):
            self.hub.ask("fake", "hi", workdir=self.work)


class WorktreeTests(HubTestCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.work, "repo")
        os.makedirs(self.repo)
        self.git_repo(self.repo)

    def run_isolated(self, prompt):
        return self.run_isolated_in(self.repo, prompt)

    def run_isolated_in(self, workdir, prompt):
        t = self.hub.start_task("fake", prompt, workdir=workdir, isolation="worktree")
        self.assertEqual(self.hub.wait_task(t["task_id"], 20)["status"], "succeeded")
        return t

    def test_isolated_edit_diff_apply(self):
        # Uncommitted user edit is visible to the task (snapshot via `git stash create`).
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("print('v2')\n")
        t = self.run_isolated("write:new.py:hello\n")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "new.py")))  # repo untouched
        iso = t["isolation"]
        with open(os.path.join(iso["path"], "app.py")) as f:
            self.assertEqual(f.read(), "print('v2')\n")
        d = self.hub.get_task_diff(t["task_id"])
        self.assertEqual(d["files"], ["A\tnew.py"])
        self.assertIn("+hello", d["patch"])
        res = self.hub.apply_task(t["task_id"])
        self.assertTrue(res["applied"])
        with open(os.path.join(self.repo, "new.py")) as f:
            self.assertEqual(f.read(), "hello\n")
        self.assertFalse(os.path.exists(iso["path"]))
        # Applying again is refused; the worktree is gone.
        with self.assertRaises(InvalidArgument):
            self.hub.apply_task(t["task_id"])

    def test_build_junk_is_not_in_diff(self):
        t = self.run_isolated("write:__pycache__/x.pyc:junk")
        self.assertEqual(self.hub.get_task_diff(t["task_id"])["files"], [])

    def test_discard(self):
        t = self.run_isolated("write:junk.py:x")
        self.hub.discard_task(t["task_id"])
        self.assertFalse(os.path.exists(t["isolation"]["path"]))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "junk.py")))

    def test_conflict_keeps_worktree(self):
        t = self.run_isolated("write:app.py:print('agent')\n")
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("print('mine')\n")
        subprocess.run(["git", "commit", "-qam", "mine"], cwd=self.repo, check=True)
        with self.assertRaises(InvalidArgument):
            self.hub.apply_task(t["task_id"])
        self.assertTrue(os.path.exists(t["isolation"]["path"]))

    def test_needs_git_repo(self):
        plain = os.path.join(self.work, "plain")
        os.makedirs(plain)
        with self.assertRaises(InvalidArgument):
            self.hub.start_task("fake", "hi", workdir=plain, isolation="worktree")

    def test_untracked_subdir_is_refused_and_cleaned_up(self):
        sub = os.path.join(self.repo, "untracked")
        os.makedirs(sub)
        with self.assertRaises(InvalidArgument) as cm:
            self.hub.start_task("fake", "hi", workdir=sub, isolation="worktree")
        self.assertIn("no tracked files", cm.exception.message)
        self.assertEqual(os.listdir(self.hub.config.worktrees_dir), [])
        self.assertEqual(self.hub.list_tasks()["tasks"], [])

    def test_subdir_workdir_maps_into_worktree(self):
        os.makedirs(os.path.join(self.repo, "pkg"))
        with open(os.path.join(self.repo, "pkg", "m.py"), "w") as f:
            f.write("x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "pkg"], cwd=self.repo, check=True)
        t = self.run_isolated_in(os.path.join(self.repo, "pkg"), "write:n.py:1")
        self.assertEqual(self.hub.get_task_diff(t["task_id"])["files"], ["A\tpkg/n.py"])

    def test_review_task_diff(self):
        t = self.run_isolated("write:new.py:hello\n")
        res = self.hub.review("fake", task_id=t["task_id"])
        out = json.loads(res["reply"])
        self.assertEqual(out["argv"][0], "ro")  # reviews are always read-only
        self.assertIn("+hello", out["prompt_head"])
        self.assertIn("new.py", res["diff_stat"])

    def test_review_uncommitted(self):
        with self.assertRaises(InvalidArgument):
            self.hub.review("fake", workdir=self.repo)  # nothing changed yet
        with open(os.path.join(self.repo, "app.py"), "a") as f:
            f.write("print('more')\n")
        out = json.loads(self.hub.review("fake", workdir=self.repo, instructions="focus on bugs")["reply"])
        self.assertIn("+print('more')", out["prompt_head"])
        self.assertIn("focus on bugs", out["prompt_head"])
        with self.assertRaises(InvalidArgument):
            self.hub.review("fake", workdir=self.repo, base="HEAD; rm -rf /")


class CompareTests(HubTestCase):
    def test_compare(self):
        res = self.hub.compare(["fake", "fake2"], "hi", workdir=self.work)
        self.assertEqual(res["succeeded"], 2)
        self.assertEqual([r["agent"] for r in res["results"]], ["fake", "fake2"])
        self.assertEqual(json.loads(res["results"][0]["reply"])["argv"][0], "ro")

    def test_compare_reports_per_agent_errors(self):
        res = self.hub.compare(["fake", "fake2"], "hi", workdir="/etc")
        self.assertEqual(res["succeeded"], 0)
        self.assertEqual(res["results"][0]["error_code"], "policy_denied")

    def test_compare_validation(self):
        with self.assertRaises(InvalidArgument):
            self.hub.compare(["fake"], "hi")
        with self.assertRaises(InvalidArgument):
            self.hub.compare(["fake", "fake"], "hi")


class TaskStoreRegressionTests(HubTestCase):
    def _finish_running_task(self):
        task = self.hub.start_task("fake", "hello", workdir=self.work)
        for _ in range(100):  # the runner writes .exit; the record still says "running" until refreshed
            if os.path.exists(self.hub.tasks._p(task["task_id"], "exit")):
                return task
            time.sleep(0.05)
        self.fail("task never wrote its exit file")

    def test_start_does_not_deadlock_on_finished_but_unrefreshed_task(self):
        import threading

        self._finish_running_task()
        result = {}

        def go():
            result["task"] = self.hub.start_task("fake", "again", workdir=self.work)

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "start_task hung: file lock is not re-entrant")
        self.assertIn("task_id", result["task"])

    def test_lock_wait_times_out_with_clear_error(self):
        import fcntl

        from agenthub import tasks as tasks_module

        fd = os.open(os.path.join(self.hub.tasks.dir, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        old = tasks_module.LOCK_TIMEOUT_SECONDS
        tasks_module.LOCK_TIMEOUT_SECONDS = 0.3
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaisesRegex(Unavailable, "task store busy"):
                self.hub.tasks.update("nope", x=1)
        finally:
            tasks_module.LOCK_TIMEOUT_SECONDS = old
            os.close(fd)

    def test_failed_start_leaves_no_schema_file(self):
        from agenthub.errors import LimitExceeded

        self.hub.tasks.max_concurrent = 0
        with self.assertRaises(LimitExceeded):
            self.hub.start_task("fake", "hi", workdir=self.work, output_schema={"type": "object"})
        leftovers = [n for n in os.listdir(self.hub.tasks.dir) if n.endswith(".schema")]
        self.assertEqual(leftovers, [])

    def test_prune_sweeps_orphan_files_only(self):
        d = self.hub.tasks.dir
        orphan, live = os.path.join(d, "fake-old.schema"), os.path.join(d, "fake-new.schema")
        for p in (orphan, live):
            open(p, "w").close()
        os.utime(orphan, (1, 1))
        self.hub.config.retention_days = 1
        self.hub.tasks.retention_days = 1
        self.hub.tasks.prune()
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(live))

    def test_structured_task_result_is_not_duplicated(self):
        task = self.hub.start_task("fake", "hi", workdir=self.work, output_schema={"type": "object"})
        done = self.hub.wait_task(task["task_id"], timeout_seconds=30)
        self.assertEqual(done["status"], "succeeded", done)
        self.assertEqual(done.get("structured"), {"answer": 42})
        self.assertNotIn("final_output", done)


class StartupTests(HubTestCase):
    def test_run_answers_handshake_while_task_lock_is_held(self):
        import fcntl
        import json as _json

        fd = os.open(os.path.join(self.hub.tasks.dir, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # A finished task still marked "running": refreshing it at startup needs the lock.
            tid = "fake-20260101-000000-deadbeef"
            meta = {
                "task_id": tid,
                "agent": "fake",
                "status": "running",
                "pid": 99999999,
                "pid_start_ticks": None,
                "workdir": self.work,
                "permission_mode": "read-only",
                "model": None,
                "prompt": {},
                "output_file": None,
                "created_at": time.time(),
                "finished_at": None,
            }
            with open(os.path.join(self.hub.tasks.dir, tid + ".json"), "w") as f:
                _json.dump(meta, f)
            with open(os.path.join(self.hub.tasks.dir, tid + ".exit"), "w") as f:
                _json.dump({"exit_code": 0, "finished_at": time.time()}, f)
            env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))
            env["AGENTHUB_HOME"] = os.path.dirname(self.hub.tasks.dir)
            proc = subprocess.Popen(
                [sys.executable, "-m", "agenthub", "mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=env,
            )
            try:
                init = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"},
                    },
                }
                proc.stdin.write(_json.dumps(init) + "\n")
                proc.stdin.flush()
                timer = threading.Timer(5, proc.kill)
                timer.start()
                line = proc.stdout.readline()
                timer.cancel()
                self.assertIn('"result"', line)
            finally:
                proc.kill()
        finally:
            os.close(fd)
