#!/usr/bin/env python3
"""Stand-in coding agent for tests. argv: <mode-flag> [options] <prompt>"""

import json
import os
import sys
import time

args = sys.argv[1:]
prompt = args[-1]
if prompt.startswith("sleep:"):
    time.sleep(float(prompt.split(":", 1)[1]))
if prompt == "fail":
    print("something broke", file=sys.stderr)
    sys.exit(3)
if "--json-out" in args:
    print(json.dumps({"result": f"json reply to {prompt[:40]}", "session_id": "sess-123", "cost": 0.01}))
    sys.exit(0)
if prompt == "quota":
    print("Error: RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1h2m3s.", file=sys.stderr)
    sys.exit(1)
if prompt.startswith("write:"):
    _, name, content = prompt.split(":", 2)
    if os.path.dirname(name):
        os.makedirs(os.path.dirname(name), exist_ok=True)
    with open(name, "w") as f:
        f.write(content)
if "--schema" in args:
    with open(args[args.index("--schema") + 1]) as f:
        json.load(f)
    print(json.dumps({"answer": 42}))
    sys.exit(0)
print(
    json.dumps(
        {
            "argv": args,
            "cwd": os.getcwd(),
            "depth": os.environ.get("AGENTHUB_DEPTH"),
            "stdin": sys.stdin.read(),
            "env_keys": sorted(os.environ),
            "prompt_len": len(prompt),
            "prompt_head": prompt[:3000],
        }
    )
)
