import unittest
from contextlib import nullcontext
from datetime import date

from asof import PostgresRevisionStore, RevisionHistory, VintageValue


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def execute(self, statement, params=None):
        self.connection.executed.append((statement, params))

    def executemany(self, statement, params):
        self.connection.executed_many.append((statement, tuple(params)))

    def fetchall(self):
        return self.connection.results.pop(0)

    def fetchone(self):
        return (1,)


class FakeConnection:
    def __init__(self):
        self.executed = []
        self.executed_many = []
        self.results = []
        self.transactions = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        self.transactions += 1
        return nullcontext()

    def close(self):
        self.closed = True


def history():
    return RevisionHistory(
        series_id="PAYEMS",
        observation_date="2008-08-01",
        revisions=(
            VintageValue("2008-09-05", 137473.0),
            VintageValue("2008-10-03", 137477.0),
        ),
    )


class PostgresRevisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = FakeConnection()
        self.store = PostgresRevisionStore(
            "postgresql://example",
            connection_factory=lambda dsn: self.connection,
        )

    def tearDown(self):
        self.store.close()

    def test_schema_is_created_on_startup(self):
        self.assertEqual(self.connection.transactions, 1)
        self.assertEqual(len(self.connection.executed), 3)

    def test_read_only_startup_does_not_create_schema(self):
        connection = FakeConnection()

        store = PostgresRevisionStore(
            "postgresql://example",
            connection_factory=lambda dsn: connection,
            read_only=True,
        )

        self.assertEqual(connection.transactions, 0)
        self.assertEqual(connection.executed, [])
        store.close()

    def test_readiness_checks_the_series_table(self):
        ready = self.store.is_ready()

        self.assertTrue(ready)
        self.assertIn("SELECT 1 FROM series", self.connection.executed[-1][0])

    def test_history_is_replaced_in_one_transaction(self):
        saved = self.store.save_history(history())

        statements = [statement for statement, _ in self.connection.executed]
        self.assertEqual(saved, 2)
        self.assertEqual(self.connection.transactions, 2)
        self.assertTrue(any("DELETE FROM vintages" in statement for statement in statements))
        self.assertEqual(len(self.connection.executed_many[0][1]), 2)

    def test_history_is_loaded_from_postgres_dates(self):
        self.connection.results.append(
            [
                (date(2008, 9, 5), 137473.0),
                (date(2008, 10, 3), 137477.0),
            ]
        )

        actual = self.store.load_history(
            series_id="PAYEMS",
            observation_date="2008-08-01",
        )

        self.assertEqual(actual, history())

    def test_observation_dates_are_returned_as_iso_strings(self):
        self.connection.results.append([(date(2008, 8, 1),), (date(2008, 9, 1),)])

        actual = self.store.list_observations(series_id="PAYEMS")

        self.assertEqual(actual, ("2008-08-01", "2008-09-01"))


if __name__ == "__main__":
    unittest.main()
