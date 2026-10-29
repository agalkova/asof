import tempfile
import unittest
from pathlib import Path

from asof import RevisionHistory, SQLiteRevisionStore, VintageValue


def history(
    observation_date="2008-08-01",
    revisions=(
        VintageValue("2008-09-05", 137473.0),
        VintageValue("2008-10-03", 137477.0),
    ),
):
    return RevisionHistory(
        series_id="PAYEMS",
        observation_date=observation_date,
        revisions=revisions,
    )


class SQLiteRevisionStoreTests(unittest.TestCase):
    def test_history_round_trip(self):
        expected = history()

        with SQLiteRevisionStore(":memory:") as store:
            saved = store.save_history(expected)
            actual = store.load_history(
                series_id="PAYEMS",
                observation_date="2008-08-01",
            )

        self.assertEqual(saved, 2)
        self.assertEqual(actual, expected)

    def test_saving_again_replaces_the_previous_snapshot(self):
        replacement = history(
            revisions=(VintageValue("2008-09-05", 137400.0),),
        )

        with SQLiteRevisionStore(":memory:") as store:
            store.save_history(history())
            store.save_history(replacement)
            actual = store.load_history(
                series_id="PAYEMS",
                observation_date="2008-08-01",
            )

        self.assertEqual(actual, replacement)

    def test_observation_dates_are_listed_in_order(self):
        with SQLiteRevisionStore(":memory:") as store:
            store.save_history(history(observation_date="2008-09-01"))
            store.save_history(history(observation_date="2008-08-01"))

            dates = store.list_observations(series_id="PAYEMS")

        self.assertEqual(dates, ("2008-08-01", "2008-09-01"))

    def test_multiple_histories_are_saved_together(self):
        histories = (
            history(observation_date="2008-08-01"),
            history(
                observation_date="2008-09-01",
                revisions=(VintageValue("2008-10-03", 136978.0),),
            ),
        )

        with SQLiteRevisionStore(":memory:") as store:
            saved = store.save_histories(histories)
            dates = store.list_observations(series_id="PAYEMS")

        self.assertEqual(saved, 3)
        self.assertEqual(dates, ("2008-08-01", "2008-09-01"))

    def test_file_database_survives_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "nested" / "asof.sqlite3"
            expected = history()
            with SQLiteRevisionStore(database) as store:
                store.save_history(expected)

            with SQLiteRevisionStore(database) as store:
                actual = store.load_history(
                    series_id="PAYEMS",
                    observation_date="2008-08-01",
                )

        self.assertEqual(actual, expected)

    def test_file_database_can_be_reopened_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "asof.sqlite3"
            expected = history()
            with SQLiteRevisionStore(database) as store:
                store.save_history(expected)

            with SQLiteRevisionStore(database, read_only=True) as store:
                actual = store.load_history(
                    series_id="PAYEMS",
                    observation_date="2008-08-01",
                )
                ready = store.is_ready()

        self.assertEqual(actual, expected)
        self.assertTrue(ready)


if __name__ == "__main__":
    unittest.main()
