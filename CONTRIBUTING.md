# Contributing

Thanks for your interest in Claude Fleet! Contributions are welcome.

## Development setup

```bash
git clone https://github.com/tianyilt/claude-fleet
cd claude-fleet
bash run.sh          # creates a venv, installs deps, starts the dashboard
```

The backend only **reads** `~/.claude/` and `~/.codex/` — it never mutates agent
state. Keep it that way: read-only access to the user's harness data is a core
invariant.

### Demo data

You don't need your own sessions to develop or take screenshots. Seed a fake
tree and point the dashboard at it with `CLAUDE_FLEET_HOME`:

```bash
python3 fixtures/seed.py
CLAUDE_FLEET_HOME=fixtures/demo-home bash run.sh
python3 fixtures/seed.py --stop   # stop the fake session processes when done
```

`docs/*.png` are generated from this demo data (`bash scripts/gen-screenshots.sh`),
never from real sessions — please keep it that way.

## Before opening a PR

- Make sure the code still compiles:
  ```bash
  python -m py_compile app.py core/*.py
  ```
- Keep the frontend dependency-free (Alpine.js + Tailwind via CDN, no npm build).
- Don't commit anything machine-specific: home paths, usernames, internal
  hostnames, API keys, or org-internal identifiers. CI and the project's
  `scripts/secrets-audit.py` will flag these.

## Reporting issues

Open a GitHub issue with your OS, Python version, and the relevant snippet from
the terminal where you ran `bash run.sh`.
