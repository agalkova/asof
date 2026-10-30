"""Run the API with production-friendly network settings."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    uvicorn.run("asof.server:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
