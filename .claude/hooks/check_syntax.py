#!/usr/bin/env python3
"""PostToolUse: after Claude edits a Python file, make sure it still compiles.

Exit 2 sends the error back to Claude so it fixes it before moving on.
"""

import json
import subprocess
import sys

from _python import project_python

payload = json.load(sys.stdin)
path = (payload.get("tool_input") or {}).get("file_path", "")
if not path.endswith(".py"):
    sys.exit(0)

result = subprocess.run(
    [project_python(), "-m", "py_compile", path],
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    print(f"{path} does not compile:\n{result.stderr.strip()}", file=sys.stderr)
    sys.exit(2)
