import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from asof import RevisionHistory, SQLiteRevisionStore, VintageValue
from asof.cli import _build_store, main
from asof.postgres import PostgresConnectionError


class CliTests(unittest.TestCase):
    @patch("asof.sync.get_revision_histories")
    @patch("asof.cli.load_dotenv")
    def test_sync_stores_the_requested_range(self, mock_load_dotenv, mock_get_histories):
        mock_get_histories.return_value = (
            RevisionHistory(
                series_id="PAYEMS",
                observation_date="2008-08-01",
                revisions=(VintageValue("2008-09-05", 137473.0),),
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "asof.sqlite3"
            output = io.StringIO()
            with patch.dict(
                os.environ,
                {"FRED_API_KEY": "test-key"},
                clear=True,
            ), redirect_stdout(output):
                result = main(
                    [
                        "sync",
                        "--series",
                        "PAYEMS",
                        "--start",
                        "2008-08-01",
                        "--end",
                        "2008-08-01",
                        "--sqlite",
                        str(database),
                    ]
                )
            with SQLiteRevisionStore(database, read_only=True) as store:
                stored = store.load_history(
                    series_id="PAYEMS",
                    observation_date="2008-08-01",
                )

        self.assertEqual(result, 0)
        self.assertIsNotNone(stored)
        self.assertIn("Stored 1 vintage across 1 PAYEMS observation", output.getvalue())
        mock_load_dotenv.assert_called_once_with()

    @patch("asof.cli.load_dotenv")
    def test_sync_requires_the_api_key(self, mock_load_dotenv):
        error = io.StringIO()

        with patch.dict(os.environ, {}, clear=True), redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "sync",
                        "--series",
                        "PAYEMS",
                        "--start",
                        "2008-08-01",
                        "--end",
                        "2008-08-01",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("FRED_API_KEY is not set", error.getvalue())

    @patch("asof.cli.load_dotenv")
    def test_sync_rejects_a_backwards_range(self, mock_load_dotenv):
        error = io.StringIO()

        with redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "sync",
                        "--series",
                        "PAYEMS",
                        "--start",
                        "2008-09-01",
                        "--end",
                        "2008-08-01",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--start must be on or before --end", error.getvalue())

    @patch("asof.cli.PostgresRevisionStore")
    @patch("asof.cli.load_dotenv")
    def test_postgres_connection_errors_do_not_expose_the_dsn(
        self,
        mock_load_dotenv,
        mock_store,
    ):
        secret = "do-not-print-this-password"
        mock_store.side_effect = PostgresConnectionError("Could not connect to PostgreSQL")
        error = io.StringIO()

        with patch.dict(
            os.environ,
            {
                "ASOF_DATABASE_URL": f"postgresql://asof:{secret}@localhost/asof",
                "FRED_API_KEY": "test-key",
            },
            clear=True,
        ), redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "sync",
                        "--series",
                        "PAYEMS",
                        "--start",
                        "2008-08-01",
                        "--end",
                        "2008-08-01",
                    ]
                )

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("Could not connect to PostgreSQL", error.getvalue())
        self.assertNotIn(secret, error.getvalue())

    @patch("asof.cli.SQLiteRevisionStore")
    def test_generic_database_url_is_ignored(self, mock_store):
        with patch.dict(
            os.environ,
            {
                "ASOF_DATABASE_PATH": "data/test.sqlite3",
                "DATABASE_URL": "postgresql://unrelated-production-database",
            },
            clear=True,
        ):
            _build_store(None)

        mock_store.assert_called_once_with("data/test.sqlite3")


if __name__ == "__main__":
    unittest.main()
