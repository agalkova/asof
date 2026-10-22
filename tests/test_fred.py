import unittest
from unittest.mock import Mock, patch

import requests

from asof.fred import FredApiError, compare_vintages, get_observation


def response_with(value, *, realtime_start="2008-09-12", realtime_end="2008-09-12"):
    response = Mock()
    response.json.return_value = {
        "observations": [
            {
                "realtime_start": realtime_start,
                "realtime_end": realtime_end,
                "date": "2008-08-01",
                "value": value,
            }
        ]
    }
    return response


class FredTests(unittest.TestCase):
    @patch("asof.fred.requests.get")
    def test_get_observation_sends_point_in_time_parameters(self, mock_get):
        mock_get.return_value = response_with("137473")

        observation = get_observation(
            series_id="PAYEMS",
            observation_date="2008-08-01",
            as_of_date="2008-09-12",
            api_key="test-key",
        )

        self.assertEqual(observation.value, 137473.0)
        params = mock_get.call_args.kwargs["params"]
        self.assertEqual(params["realtime_start"], "2008-09-12")
        self.assertEqual(params["realtime_end"], "2008-09-12")

    @patch("asof.fred.requests.get")
    def test_compare_vintages_calculates_current_minus_historical(self, mock_get):
        mock_get.side_effect = [
            response_with("137473"),
            response_with(
                "137211",
                realtime_start="2026-10-22",
                realtime_end="2026-10-22",
            ),
        ]

        comparison = compare_vintages(
            series_id="PAYEMS",
            observation_date="2008-08-01",
            historical_as_of="2008-09-12",
            api_key="test-key",
        )

        self.assertEqual(comparison.historical.value, 137473.0)
        self.assertEqual(comparison.current.value, 137211.0)
        self.assertEqual(comparison.revision, -262.0)
        current_params = mock_get.call_args_list[1].kwargs["params"]
        self.assertNotIn("realtime_start", current_params)
        self.assertNotIn("realtime_end", current_params)

    @patch("asof.fred.requests.get")
    def test_missing_observation_has_a_clear_error(self, mock_get):
        response = Mock()
        response.json.return_value = {"observations": []}
        mock_get.return_value = response

        with self.assertRaisesRegex(ValueError, "Expected one PAYEMS observation"):
            get_observation(
                series_id="PAYEMS",
                observation_date="1900-01-01",
                api_key="test-key",
            )

    @patch("asof.fred.requests.get")
    def test_api_error_does_not_add_the_key_to_our_error_message(self, mock_get):
        response = requests.Response()
        response.status_code = 401
        response.url = (
            "https://api.stlouisfed.org/fred/series/observations"
            "?series_id=PAYEMS&api_key=secret-test-key"
        )
        mock_get.return_value = response

        with self.assertRaises(FredApiError) as caught:
            get_observation(
                series_id="PAYEMS",
                observation_date="2008-08-01",
                api_key="secret-test-key",
            )

        self.assertNotIn("secret-test-key", str(caught.exception))
        self.assertEqual(str(caught.exception), "FRED request failed with HTTP status 401")


if __name__ == "__main__":
    unittest.main()
