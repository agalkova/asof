#!/usr/bin/env python3
"""Stop: when Python files changed, run the test suite before Claude says it's done.

Exit 2 keeps Claude working with the failure in front of it. After one forced
retry (stop_hook_active) it lets Claude stop, so it can report a failure it
can't fix instead of looping.
"""

import json
import subprocess
import sys

from _python import project_python, project_root

payload = json.load(sys.stdin)
if payload.get("stop_hook_active"):
    sys.exit(0)

root = project_root()
changed = subprocess.run(
    ["git", "status", "--porcelain", "--", "src", "tests"],
    cwd=root,
    capture_output=True,
    text=True,
).stdout
if ".py" not in changed:
    sys.exit(0)

python = project_python()
missing = subprocess.run(
    [python, "-c", "import boto3, moto"],
    cwd=root,
    capture_output=True,
).returncode
result = subprocess.run(
    [python, "-m", "unittest", "discover", "-s", "tests"],
    cwd=root,
    capture_output=True,
    text=True,
    timeout=300,
)
tail = "\n".join(result.stderr.strip().splitlines()[-40:])
if result.returncode != 0:
    print(f"The test suite fails. Fix it before finishing:\n{tail}", file=sys.stderr)
    sys.exit(2)
if missing:
    print(
        "Tests passed, but the DynamoDB tests were skipped because boto3/moto aren't "
        "installed. Run: pip install -e \".[api,postgres,dynamodb,dev]\"",
        file=sys.stderr,
    )
    sys.exit(2)
