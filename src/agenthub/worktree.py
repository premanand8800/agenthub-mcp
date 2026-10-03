"""Git worktree isolation: a task edits its own checkout, so it cannot clash with your edits.

Flow: create (from your current state, including uncommitted tracked changes) -> agent works ->
diff -> apply to your repo, or discard.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict

from agenthub.errors import InvalidArgument
from agenthub.process import git


def repo_root(path: str) -> str:
    try:
        return git(["rev-parse", "--show-toplevel"], path).strip()
    except (RuntimeError, FileNotFoundError) as e:
        raise InvalidArgument(f"isolation='worktree' needs a git repository; '{path}' is not in one ({e})") from e


def create(workdir: str, dest: str) -> Dict[str, Any]:
    """Create a detached worktree at `dest` mirroring `workdir`'s repo. Returns isolation metadata."""
    root = repo_root(workdir)
    # `stash create` snapshots tracked changes as a commit without touching your working tree.
    base = git(["stash", "create"], root).strip()
    note = "includes your uncommitted changes to tracked files"
    if not base:
        try:
            base = git(["rev-parse", "HEAD"], root).strip()
        except RuntimeError as e:
            raise InvalidArgument(f"Repository at '{root}' has no commits yet") from e
        note = "clean checkout of HEAD"
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    git(["worktree", "add", "--detach", dest, base], root)
    rel = os.path.relpath(workdir, root)
    cwd = os.path.normpath(os.path.join(dest, rel))
    if not os.path.isdir(cwd):
        remove({"repo": root, "path": dest, "state": "active"})
        raise InvalidArgument(
            f"'{rel}' has no tracked files, so it does not exist in the worktree. "
            "Use the repository root as workdir, or add the directory to git first."
        )
    return {
        "type": "worktree",
        "repo": root,
        "path": dest,
        "base": base,
        "cwd": cwd,
        "note": note + "; untracked files are not copied",
        "state": "active",
    }


# Build artifacts agents create while testing. Excluded even when the repo has no .gitignore.
JUNK = ("__pycache__", "*.pyc", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store")


def _stage(iso: Dict[str, Any]) -> None:
    # Stages in the worktree's own index; your repo's index is untouched.
    excludes = [f":(exclude,glob)**/{p}" for p in JUNK] + [f":(exclude,glob)**/{p}/**" for p in JUNK]
    git(["add", "-A", "--", ".", *excludes], iso["path"])


def diff(iso: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    if iso.get("state") != "active" or not os.path.isdir(iso["path"]):
        raise InvalidArgument(f"Worktree is {iso.get('state', 'gone')}; no diff available")
    _stage(iso)
    stat = git(["diff", "--cached", "--stat", iso["base"]], iso["path"]).strip()
    names = git(["diff", "--cached", "--name-status", iso["base"]], iso["path"]).split("\n")
    patch = git(["diff", "--cached", iso["base"]], iso["path"])
    return {
        "files": [n for n in names if n.strip()],
        "stat": stat,
        "patch": patch
        if len(patch) <= max_chars
        else patch[:max_chars] + f"\n[... patch truncated at {max_chars} chars; see the worktree at {iso['path']} ...]",
        "truncated": len(patch) > max_chars,
        "worktree": iso["path"],
    }


def apply(iso: Dict[str, Any]) -> Dict[str, Any]:
    """Apply the task's changes to the original repo's working tree."""
    if iso.get("state") != "active":
        raise InvalidArgument(f"Worktree is {iso.get('state')}; nothing to apply")
    _stage(iso)
    patch = git(["diff", "--cached", "--binary", iso["base"]], iso["path"])
    if not patch.strip():
        return {"applied": False, "message": "The task made no changes"}
    try:
        git(["apply", "--whitespace=nowarn", "-"], iso["repo"], input_text=patch)
        how = "clean"
    except RuntimeError:
        # Your files changed since the task started. Merge with the task's base as common ancestor.
        try:
            git(["apply", "--3way", "--whitespace=nowarn", "-"], iso["repo"], input_text=patch)
            how = "3way"
        except RuntimeError as e:
            raise InvalidArgument(
                f"Patch does not apply cleanly; resolve by hand or discard. Worktree kept at {iso['path']}. "
                f"git said: {e}"
            ) from e
    return {"applied": True, "method": how, "message": "Changes are in your working tree (not committed)"}


def remove(iso: Dict[str, Any]) -> None:
    if iso.get("state") == "removed":
        return
    try:
        git(["worktree", "remove", "--force", iso["path"]], iso["repo"])
    except (RuntimeError, FileNotFoundError):
        shutil.rmtree(iso["path"], ignore_errors=True)
        git(["worktree", "prune"], iso["repo"], check=False)
