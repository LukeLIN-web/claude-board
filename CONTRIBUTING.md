# Contributing

Thanks for your interest in Claude Fleet! Contributions are welcome.

## Development setup

```bash
git clone https://github.com/LukeLIN-web/claude-board
cd claude-board
bash run.sh          # creates a venv, installs deps, starts the dashboard
```

The backend never writes to the stored harness data under `~/.claude/` and
`~/.codex/`; user-triggered actions act on live sessions only. Keep it that way.

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

- Run what CI runs; the header of `.github/workflows/ci.yml` lists the commands.
- If you changed how the board reads or drives a Claude Code screen, also run
  `pytest tests/live --run-live -v` on a machine with `claude` and `tmux` (two
  short haiku turns).
- Keep the frontend dependency-free (Alpine.js + Tailwind via CDN, no npm build).
- Don't commit anything machine-specific: home paths, usernames, internal
  hostnames, API keys, or org-internal identifiers. `scripts/secrets-audit.py`
  flags these, in CI and locally. Names private to your setup go in
  `SECRETS_AUDIT_PATTERNS` (one regex per line) rather than in the script.

## Reporting issues

Open a GitHub issue with your OS, Python version, and the relevant snippet from
the board's log: `bash run.sh` runs it detached, so its output goes to
`~/.local/state/claude-fleet/board.log` (under `$XDG_STATE_HOME` if you set
it), not to your terminal.
