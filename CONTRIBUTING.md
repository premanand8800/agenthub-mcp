# Contributing

Thanks for helping. AgentHub has two rules: no runtime dependencies (standard library only), and every
agent goes through the same policy layer in `Hub`.

## Setup

```bash
git clone https://github.com/premanand8800/agenthub-mcp && cd agenthub-mcp
PYTHONPATH=src python -m unittest discover -s tests -t tests -v   # no API keys or quota needed
uvx ruff check src tests && uvx ruff format --check src tests
```

## Adding an agent

1. Create `src/agenthub/adapters/<name>.py` with a subclass of `Adapter`.
2. Implement `build_command(spec)`, which returns argv only. Never build a shell string, and make sure a
   prompt can't be parsed as a CLI option (use `--`, `--flag=value` or `safe_positional`).
3. Declare `modes`: only the permission modes the CLI really enforces.
4. Declare `env_allow`: the environment variables the CLI needs (its API key, config dir).
5. If the CLI prints JSON, implement `parse_output` so callers get `reply`, `session_id` and `usage`.
6. Register it in `adapters/__init__.py` and add argv tests in `tests/test_adapters.py`.
7. Note the CLI version you tested against in the PR.

## Pull requests

- Add tests for behavior changes. Tests use `tests/fake_agent.py`, not real agents.
- Run CI checks locally first (tests + ruff).
- Security-sensitive changes (paths, env, argv, HTTP) need a test that shows the attack being blocked.
- Report vulnerabilities privately (see SECURITY.md), not in issues.
