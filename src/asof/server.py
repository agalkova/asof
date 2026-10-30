"""Default local server configuration."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from .api import create_app
from .postgres import PostgresRevisionStore
from .storage import SQLiteRevisionStore

load_dotenv()


def build_store():
    database_url = os.getenv("ASOF_DATABASE_URL")
    if database_url:
        return PostgresRevisionStore(database_url, read_only=True)
    return SQLiteRevisionStore(
        os.getenv("ASOF_DATABASE_PATH", "data/asof.sqlite3"),
        read_only=True,
    )


app = create_app(build_store(), close_store=True)
