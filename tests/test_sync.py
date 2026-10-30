import unittest
from unittest.mock import patch

from asof import RevisionHistory, SQLiteRevisionStore, VintageValue
from asof.sync import sync_revision_histories


class SyncTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
