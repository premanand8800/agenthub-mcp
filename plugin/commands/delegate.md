---
description: Hand a coding task to another agent in an isolated git worktree, then review and apply it
argument-hint: <agent> <task description>
---

Delegate this work: $ARGUMENTS
The first word is the agent to use (for example codex); the rest is the task.

1. Call the agenthub `start_task` tool with that agent, the task as `prompt`, `workdir` set to the current
   git repository, and `isolation` set to "worktree". Write a precise prompt: goal, files involved, constraints,
   and how to verify (tests to run).
2. Tell the user the task ID, then call `wait_task` (repeat while `finished` is false).
3. When it finishes, call `get_task_diff` and review the patch carefully. Optionally call `review` with
   `task_id` to get a second model's review.
4. Show the user a short summary of the changes and your assessment. Call `apply_task` only after the user
   agrees; otherwise call `discard_task`.
