#!/usr/bin/env python3
"""Fail when a tracked file carries a credential or something machine-specific.

This repository is public, and most of what it tests is text captured from real
terminals: a pane, a banner, a status line. Each capture carries whatever the
machine it came from printed, such as the path of the project, the user's
name in it, or a host. This walks every file git tracks or would track and refuses:

  - credentials: API keys and tokens of the common providers, private keys
  - real home and cluster paths: /home/<name>/ and /Users/<name>/ other than
    the placeholders the fixtures use, and /shared/userNN mounts, in path form
    and in the dashed form Claude uses for project directories
  - email addresses, other than example and noreply ones

What is private to one setup (host names, user names) cannot be written here
without publishing it, so it comes from the environment instead:
SECRETS_AUDIT_PATTERNS holds one more regex per line. CI reads it from a
repository secret of the same name; locally, export it or put it in
.env.local. A line that has to keep a match ends with `secrets-audit: allow`.

    python3 scripts/secrets-audit.py          # exit 1 and list the hits
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# Names the fixtures use in place of a real account.
_PLACEHOLDER_USERS = r"(?:u|dev|user|me|you|runner|alice|bob|example|USER|<[^>]+>)"

PATTERNS: list[tuple[str, str]] = [
    ("Anthropic key", r"sk-ant-[A-Za-z0-9_-]{16,}"),
    ("OpenAI key", r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}"),
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    ("AWS key id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # "/home/name/x" and Claude's project-dir form of it, "-home-name-x".
    ("home path", rf"(?:/(?:home|Users)/(?!{_PLACEHOLDER_USERS}(?:/|\b))[A-Za-z0-9_.-]+"
                  rf"|-(?:home|Users)-(?!{_PLACEHOLDER_USERS}(?:-|\b))[A-Za-z0-9_.]+)"),
    ("cluster mount", r"[/-]shared[/-]user\d+"),
    ("email address",
     r"\b[A-Za-z0-9._%+-]+@(?!(?:[A-Za-z0-9-]+\.)*(?:example\.(?:com|org|net)"
     r"|users\.noreply\.github\.com|anthropic\.com)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
]

ALLOW_MARK = "secrets-audit: allow"


def _extra_patterns() -> list[tuple[str, str]]:
    raw = os.environ.get("SECRETS_AUDIT_PATTERNS", "")
    return [("private pattern", ln.strip()) for ln in raw.splitlines() if ln.strip()]


def _tracked_files() -> list[str]:
    # Untracked files too (minus ignored ones), so a new fixture is caught before
    # it is ever added.
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, check=True)
    return [p for p in out.stdout.decode().split("\0") if p]


def main() -> int:
    patterns = [(label, re.compile(rx)) for label, rx in PATTERNS + _extra_patterns()]
    hits = 0
    for path in _tracked_files():
        if path == "scripts/secrets-audit.py":
            continue  # its own patterns would match themselves
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            continue  # deleted in the working tree but still in the index
        if b"\0" in data[:8192]:
            continue  # binary
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if ALLOW_MARK in line:
                continue
            for label, rx in patterns:
                m = rx.search(line)
                if m:
                    hits += 1
                    # Private patterns name what they match; don't echo it.
                    shown = "…" if label == "private pattern" else m.group(0)
                    print(f"{path}:{n}: {label}: {shown}")
    if hits:
        print(f"\n✗ {hits} hit(s). Replace them with a placeholder "
              "(/home/u/…, example.com), or end the line with "
              f"`# {ALLOW_MARK}` if it has to stay.", file=sys.stderr)
        return 1
    print("✓ no credentials or machine-specific strings in tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
