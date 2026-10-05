# Contributing

Thanks for your interest in Claude Fleet! Contributions are welcome.

## Development setup

```bash
git clone https://github.com/LukeLIN-web/claude-board
cd claude-board
bash run.sh          # creates a venv, installs deps, starts the dashboard
```

The backend never writes to the user's stored harness data under `~/.claude/` and
`~/.codex/` — that read-only access is a core invariant; keep it that way. Fleet is
read-only **by default**: a few explicit, user-triggered actions (fork, close,
review, and tmux-backed session spawn / single-prompt injection on Linux) act on
live sessions, but they must never read-modify-write the stored harness data.

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

- Run what CI runs (`.github/workflows/ci.yml`):
  ```bash
  pip install -e '.[dev]'
  ruff check .                     # pyflakes-level: unused/undefined names
  python scripts/secrets-audit.py  # credentials and machine-specific strings
  pytest                           # runs against an empty temporary HOME
  bash scripts/smoke.sh            # boots the board on demo data, renders it in headless Chrome
  ```
- If you changed how the board reads or drives a Claude Code screen (spawn, send,
  menus, overlays, the model picker), also run the live tests on a machine with
  `claude` and `tmux`. They spawn a haiku session on a private tmux server and
  cost two short turns:
  ```bash
  pytest tests/live --run-live -v
  ```
  A failure leaves the pane in `.live-captures/`; cut a unit-test fixture from it.
- Keep the frontend dependency-free (Alpine.js + Tailwind via CDN, no npm build).
- Don't commit anything machine-specific: home paths, usernames, internal
  hostnames, API keys, or org-internal identifiers. `scripts/secrets-audit.py`
  flags these, in CI and locally. Names private to your setup go in
  `SECRETS_AUDIT_PATTERNS` (one regex per line) rather than in the script.

## Reporting issues

Open a GitHub issue with your OS, Python version, and the relevant snippet from
the terminal where you ran `bash run.sh`.
