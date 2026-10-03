---
description: Have a different model review your uncommitted changes before you commit
argument-hint: [agent] [base-ref]
---

Get an independent code review of the current changes. Arguments (optional): $ARGUMENTS
The first argument is the reviewing agent and the second is the git base ref (default HEAD).

1. If no agent was given, call `list_agents` and pick an installed agent other than yourself (prefer codex).
2. Call the agenthub `review` tool with that agent, `workdir` set to the current project directory, and the base ref.
   If the agent is out of quota, pass other installed agents as `fallback_agents`.
3. Check each finding against the code yourself. List the ones that are real, with file:line and a fix.
   Say which findings you rejected and why. Do not edit files unless the user asks.
