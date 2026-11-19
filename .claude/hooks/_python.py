"""Find the project's Python: the repo's .venv when it exists, otherwise python3."""

import os
import shutil
from pathlib import Path


def project_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])


def project_python() -> str:
    root = project_root()
    for candidate in (root / ".venv" / "bin" / "python", root / ".venv" / "Scripts" / "python.exe"):
        if candidate.exists():
            return str(candidate)
    return shutil.which("python3") or shutil.which("python") or "python3"
