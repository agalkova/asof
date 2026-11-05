import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from asof import RevisionHistory, SQLiteRevisionStore, VintageValue, __version__
from asof.api import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteRevisionStore(":memory:")
        self.store.save_histories(
            (
                RevisionHistory(
                    series_id="PAYEMS",
                    observation_date="2008-08-01",
                    revisions=(
                        VintageValue("2008-09-05", 137473.0),
                        VintageValue("2008-10-03", 137477.0),
                    ),
                ),
                RevisionHistory(
                    series_id="PAYEMS",
                    observation_date="2008-09-01",
                    revisions=(VintageValue("2008-10-03", 136978.0),),
                ),
            )
        )
        self.client = TestClient(create_app(self.store))

    def tearDown(self):
        self.client.close()
        self.store.close()

    def test_health(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_returns_503_when_storage_is_unavailable(self):
        with patch.object(self.store, "is_ready", return_value=False):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Storage is unavailable"})

    def test_observations_are_listed_in_order(self):
        response = self.client.get("/series/PAYEMS/observations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "series_id": "PAYEMS",
                "observations": ["2008-08-01", "2008-09-01"],
            },
        )

    def test_revision_history_includes_summary_and_vintages(self):
        response = self.client.get("/series/PAYEMS/observations/2008-08-01")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["initial_value"], 137473.0)
        self.assertEqual(payload["latest_value"], 137477.0)
        self.assertEqual(payload["total_revision"], 4.0)
        self.assertEqual(len(payload["revisions"]), 2)

    def test_value_as_of_uses_the_latest_available_vintage(self):
        with patch.object(
            self.store,
            "load_history",
            side_effect=AssertionError("point lookup loaded the full history"),
        ):
            response = self.client.get(
                "/series/PAYEMS/observations/2008-08-01/value",
                params={"as_of": "2008-09-12"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["vintage_date"], "2008-09-05")
        self.assertEqual(response.json()["value"], 137473.0)

    def test_value_as_of_returns_404_before_the_first_release(self):
        response = self.client.get(
            "/series/PAYEMS/observations/2008-08-01/value",
            params={"as_of": "2008-09-04"},
        )

        self.assertEqual(response.status_code, 404)

    def test_missing_history_returns_404(self):
        response = self.client.get("/series/PAYEMS/observations/1999-01-01")

        self.assertEqual(response.status_code, 404)

    def test_invalid_date_returns_422(self):
        response = self.client.get("/series/PAYEMS/observations/not-a-date")

        self.assertEqual(response.status_code, 422)

    def test_invalid_series_id_returns_422(self):
        response = self.client.get("/series/BAD!ID/observations")

        self.assertEqual(response.status_code, 422)

    def test_openapi_uses_the_package_version(self):
        response = self.client.get("/openapi.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["info"]["version"], __version__)


if __name__ == "__main__":
    unittest.main()
