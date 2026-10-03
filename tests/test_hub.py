import json
import os
import time

from helpers import HubTestCase

from agenthub.errors import InvalidArgument, LimitExceeded, NotFound, PolicyError, Unavailable


class AskTests(HubTestCase):
    def ask(self, prompt="hello", **kw):
        kw.setdefault("workdir", self.work)
        return self.hub.ask("fake", prompt, **kw)

    def test_default_mode_is_workspace_write(self):
        res = self.ask()
        self.assertTrue(res["ok"], res)
        out = json.loads(res["reply"])
        self.assertEqual(out["argv"], ["ww", "hello"])
        self.assertEqual(out["cwd"], self.work)

    def test_child_cannot_read_parent_stdin(self):
        self.assertEqual(json.loads(self.ask()["reply"])["stdin"], "")

    def test_delegation_depth_is_passed_and_enforced(self):
        self.assertEqual(json.loads(self.ask()["reply"])["depth"], "1")
        os.environ["AGENTHUB_DEPTH"] = "2"
        with self.assertRaises(PolicyError):
            self.ask()

    def test_full_mode_needs_opt_in(self):
        with self.assertRaises(PolicyError) as cm:
            self.ask(permission_mode="full")
        self.assertIn("allow_full_access", cm.exception.message)

    def test_read_only_mode(self):
        self.assertEqual(json.loads(self.ask(permission_mode="read-only")["reply"])["argv"][0], "ro")

    def test_invalid_mode(self):
        with self.assertRaises(InvalidArgument):
            self.ask(permission_mode="yolo")

    def test_prompt_starting_with_dash_is_not_an_option(self):
        argv = json.loads(self.ask("--rm-rf")["reply"])["argv"]
        self.assertEqual(argv[-1], " --rm-rf")

    def test_model_argument(self):
        argv = json.loads(self.ask(model="gpt-x")["reply"])["argv"]
        self.assertIn("--model=gpt-x", argv)
        with self.assertRaises(InvalidArgument):
            self.ask(model="--evil")

    def test_workdir_policy(self):
        with self.assertRaises(PolicyError):
            self.ask(workdir="/etc")
        with self.assertRaises(PolicyError):
            self.ask(workdir=self.secret)
        with self.assertRaises(PolicyError):
            self.ask(workdir=os.path.join(self.work, "..", ".."))
        with self.assertRaises(InvalidArgument):
            self.ask(workdir="relative/path")
        link = os.path.join(self.work, "escape")
        os.symlink("/etc", link)
        with self.assertRaises(PolicyError):
            self.ask(workdir=link)

    def test_failure_is_reported(self):
        res = self.ask("fail")
        self.assertFalse(res["ok"])
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["exit_code"], 3)
        self.assertIn("something broke", res["error"])

    def test_timeout(self):
        self.hub.config.ask_timeout_seconds = 1
        res = self.ask("sleep:10")
        self.assertEqual(res["status"], "timed_out")
        self.assertFalse(res["ok"])

    def test_unknown_agent(self):
        with self.assertRaises(NotFound):
            self.hub.ask("nope", "hi", workdir=self.work)

    def test_resume_unsupported_for_custom_agent(self):
        with self.assertRaises(Unavailable):
            self.ask(session_id="abc")

    def test_empty_and_oversized_prompt(self):
        with self.assertRaises(InvalidArgument):
            self.ask("   ")
        self.hub.config.max_prompt_chars = 10
        with self.assertRaises(InvalidArgument):
            self.ask("x" * 11)

    def test_audit_records_and_verifies(self):
        self.ask()
        entries = self.hub.audit.tail(10)
        self.assertEqual([e["status"] for e in entries], ["started", "succeeded"])
        self.assertEqual(entries[0]["details"]["prompt"]["preview"], "hello")
        ok, n, _ = self.hub.audit.verify()
        self.assertTrue(ok)
        self.assertEqual(n, 2)
        self.assertEqual(os.stat(self.hub.config.audit_path).st_mode & 0o777, 0o600)

    def test_audit_detects_tampering(self):
        self.ask()
        self.ask()
        path = self.hub.audit.path
        with open(path) as f:
            lines = f.readlines()
        with open(path, "w") as f:
            f.writelines(lines[:1] + lines[2:])  # delete one entry
        ok, _, problem = self.hub.audit.verify()
        self.assertFalse(ok)
        self.assertIn("chain broken", problem)


class TaskTests(HubTestCase):
    def wait(self, task_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = self.hub.get_task(task_id)
            if t["status"] != "running":
                return t
            time.sleep(0.1)
        self.fail("task did not finish")

    def test_lifecycle(self):
        task = self.hub.start_task("fake", "hello", workdir=self.work)
        self.assertEqual(task["status"], "running")
        done = self.wait(task["task_id"])
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["exit_code"], 0)
        self.assertIn('"hello"', done["final_output"])
        # One ID everywhere: the audit log names the same task.
        audited = [e for e in self.hub.audit.tail(5) if e["action"] == "start_task"]
        self.assertEqual(audited[-1]["details"]["task_id"], task["task_id"])

    def test_failed_task(self):
        task = self.hub.start_task("fake", "fail", workdir=self.work)
        self.assertEqual(self.wait(task["task_id"])["status"], "failed")

    def test_cancel_kills_process(self):
        task = self.hub.start_task("fake", "sleep:30", workdir=self.work)
        res = self.hub.cancel_task(task["task_id"])
        self.assertEqual(res["status"], "cancelled")
        with self.assertRaises(ProcessLookupError):
            for _ in range(30):
                os.kill(task["pid"], 0)
                time.sleep(0.1)

    def test_task_timeout(self):
        self.hub.tasks.timeout_seconds = 1
        task = self.hub.start_task("fake", "sleep:30", workdir=self.work)
        self.assertEqual(self.wait(task["task_id"])["status"], "timed_out")

    def test_concurrency_limit(self):
        self.hub.tasks.max_concurrent = 1
        t = self.hub.start_task("fake", "sleep:30", workdir=self.work)
        self.addCleanup(self.hub.cancel_task, t["task_id"])
        with self.assertRaises(LimitExceeded):
            self.hub.start_task("fake", "hi", workdir=self.work)

    def test_logs_and_listing(self):
        task = self.hub.start_task("fake", "hello", workdir=self.work)
        self.wait(task["task_id"])
        self.assertIn("hello", self.hub.task_logs(task["task_id"])["logs"])
        ids = [t["task_id"] for t in self.hub.list_tasks()["tasks"]]
        self.assertIn(task["task_id"], ids)
        self.assertEqual(self.hub.list_tasks(status="running")["tasks"], [])

    def test_task_files_are_private(self):
        task = self.hub.start_task("fake", "hello", workdir=self.work)
        self.wait(task["task_id"])
        self.assertEqual(os.stat(self.hub.config.tasks_dir).st_mode & 0o777, 0o700)
        for name in os.listdir(self.hub.config.tasks_dir):
            mode = os.stat(os.path.join(self.hub.config.tasks_dir, name)).st_mode & 0o777
            self.assertEqual(mode & 0o077, 0, name)

    def test_bad_task_id(self):
        with self.assertRaises(InvalidArgument):
            self.hub.get_task("../../etc/passwd")
        with self.assertRaises(NotFound):
            self.hub.get_task("fake-missing")
