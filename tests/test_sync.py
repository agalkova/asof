import unittest
from unittest.mock import patch

from asof import (
    RevisionConflictError,
    RevisionHistory,
    SQLiteRevisionStore,
    VintageValue,
)
from asof.sync import sync_revision_histories


class SyncTests(unittest.TestCase):
    @staticmethod
    def history(*revisions):
        return RevisionHistory(
            series_id="PAYEMS",
            observation_date="2008-08-01",
            revisions=tuple(revisions),
        )

    @patch("asof.sync.get_revision_histories_chunked")
    def test_sync_fetches_and_stores_the_requested_range(self, mock_get_histories):
        mock_get_histories.return_value = (
            RevisionHistory(
                series_id="PAYEMS",
                observation_date="2008-08-01",
                revisions=(VintageValue("2008-09-05", 137473.0),),
            ),
            RevisionHistory(
                series_id="PAYEMS",
                observation_date="2008-09-01",
                revisions=(VintageValue("2008-10-03", 136978.0),),
            ),
        )

        with SQLiteRevisionStore(":memory:") as store:
            result = sync_revision_histories(
                store=store,
                series_id="PAYEMS",
                observation_start="2008-08-01",
                observation_end="2008-09-01",
                api_key="test-key",
            )
            dates = store.list_observations(series_id="PAYEMS")

        self.assertEqual(result.observations, 2)
        self.assertEqual(result.vintages, 2)
        self.assertEqual(dates, ("2008-08-01", "2008-09-01"))
        mock_get_histories.assert_called_once_with(
            series_id="PAYEMS",
            observation_start="2008-08-01",
            observation_end="2008-09-01",
            api_key="test-key",
        )

    @patch("asof.sync.get_revision_histories_chunked")
    def test_sync_accepts_a_newer_history_that_preserves_stored_vintages(
        self, mock_get_histories
    ):
        first = VintageValue("2008-09-05", 137473.0)
        newer = VintageValue("2008-10-03", 137477.0)
        mock_get_histories.return_value = (self.history(first, newer),)

        with SQLiteRevisionStore(":memory:") as store:
            store.save_history(self.history(first))

            sync_revision_histories(
                store=store,
                series_id="PAYEMS",
                observation_start="2008-08-01",
                observation_end="2008-08-01",
                api_key="test-key",
            )

            self.assertEqual(
                store.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
                self.history(first, newer),
            )

    @patch("asof.sync.get_revision_histories_chunked")
    def test_sync_rejects_a_history_that_drops_a_stored_vintage(self, mock_get_histories):
        first = VintageValue("2008-09-05", 137473.0)
        newer = VintageValue("2008-10-03", 137477.0)
        stored = self.history(first, newer)
        mock_get_histories.return_value = (self.history(first),)

        with SQLiteRevisionStore(":memory:") as store:
            store.save_history(stored)

            with self.assertRaisesRegex(RevisionConflictError, "remove or change"):
                sync_revision_histories(
                    store=store,
                    series_id="PAYEMS",
                    observation_start="2008-08-01",
                    observation_end="2008-08-01",
                    api_key="test-key",
                )

            self.assertEqual(
                store.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
                stored,
            )

    @patch("asof.sync.get_revision_histories_chunked")
    def test_sync_rejects_a_changed_stored_vintage(self, mock_get_histories):
        stored = self.history(VintageValue("2008-09-05", 137473.0))
        mock_get_histories.return_value = (
            self.history(VintageValue("2008-09-05", 1.0)),
        )

        with SQLiteRevisionStore(":memory:") as store:
            store.save_history(stored)

            with self.assertRaises(RevisionConflictError):
                sync_revision_histories(
                    store=store,
                    series_id="PAYEMS",
                    observation_start="2008-08-01",
                    observation_end="2008-08-01",
                    api_key="test-key",
                )

            self.assertEqual(
                store.load_history(series_id="PAYEMS", observation_date="2008-08-01"),
                stored,
            )


if __name__ == "__main__":
    unittest.main()
