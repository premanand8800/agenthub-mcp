---
description: Show AgentHub background tasks and their status
---

Call the agenthub `list_tasks` tool and show a compact table: task ID, agent, status, elapsed time and workdir.
For any task that is running, offer `wait_task` or `get_task_logs`. For finished worktree tasks that were
neither applied nor discarded, remind the user they can review them with `get_task_diff`.
