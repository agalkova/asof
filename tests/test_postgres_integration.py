import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from asof import PostgresRevisionStore, RevisionHistory, VintageValue

TEST_DATABASE_URL = os.getenv("ASOF_TEST_DATABASE_URL")


def history(series_id, observation_date):
    return RevisionHistory(
        series_id=series_id,
        observation_date=observation_date,
        revisions=(
            VintageValue("2008-09-05", 137473.0),
            VintageValue("2008-10-03", 137477.0),
        ),
    )


@unittest.skipUnless(
    TEST_DATABASE_URL,
    "ASOF_TEST_DATABASE_URL is not configured",
)
class PostgresIntegrationTests(unittest.TestCase):
    def test_round_trip_survives_reopening(self):
        series_id = f"TEST_{uuid4().hex}"
        expected = history(series_id, "2008-08-01")

        with PostgresRevisionStore(TEST_DATABASE_URL) as store:
            store.save_history(expected)

        with PostgresRevisionStore(TEST_DATABASE_URL) as store:
            actual = store.load_history(
                series_id=series_id,
                observation_date="2008-08-01",
            )
            lookup = store.lookup_value_as_of(
                series_id=series_id,
                observation_date="2008-08-01",
                as_of_date="2008-09-12",
            )

        self.assertEqual(actual, expected)
        self.assertTrue(lookup.observation_exists)
        self.assertEqual(lookup.revision, VintageValue("2008-09-05", 137473.0))

    def test_failed_batch_is_rolled_back(self):
        series_id = f"TEST_{uuid4().hex}"
        first = history(series_id, "2008-08-01")
        second = history(series_id, "2008-09-01")
        replace = PostgresRevisionStore._replace_history

        def fail_on_second(cursor, item):
            replace(cursor, item)
            if item.observation_date == second.observation_date:
                raise RuntimeError("forced failure")

        with PostgresRevisionStore(TEST_DATABASE_URL) as store:
            with patch.object(
                PostgresRevisionStore,
                "_replace_history",
                side_effect=fail_on_second,
            ):
                with self.assertRaisesRegex(RuntimeError, "forced failure"):
                    store.save_histories((first, second))

            actual = store.load_history(
                series_id=series_id,
                observation_date=first.observation_date,
            )

        self.assertIsNone(actual)


if __name__ == "__main__":
    unittest.main()
