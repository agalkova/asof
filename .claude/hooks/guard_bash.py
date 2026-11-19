#!/usr/bin/env python3
"""PreToolUse on Bash: block commands that break this repo's git rules.

Exit 2 blocks the command and tells Claude why.
"""

import json
import re
import sys

command = (json.load(sys.stdin).get("tool_input") or {}).get("command", "")

RULES = [
    (r"\bgh\s+pr\s+merge\b", "Never merge pull requests. Anastasia merges."),
    (r"\bgit\s+push\b.*(\s--force(-with-lease)?\b|\s-f\b)", "Never force-push."),
    (r"\bgit\s+push\b.*\b(origin\s+)?(HEAD:)?main\b", "Never push to main. Push a branch and open a PR."),
    (r"\b(cdk|sam)\s+deploy\b|\baws\s+cloudformation\s+deploy\b", "Never deploy. Anastasia deploys."),
    (r"\bgit\s+commit\b.*\s(-S|--gpg-sign)\b", "Commits must not be signed."),
    (
        r"co-authored-by|generated with|claude\.ai/code|claude-session",
        "Commits and PRs must not carry tool attribution or session links.",
    ),
]

for pattern, reason in RULES:
    if re.search(pattern, command, re.IGNORECASE):
        print(f"Blocked: {reason}", file=sys.stderr)
        sys.exit(2)
