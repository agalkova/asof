import os
import unittest
from unittest.mock import patch

from asof.run_server import main


class RunServerTests(unittest.TestCase):
    @patch("asof.run_server.uvicorn.run")
    def test_uses_the_host_port(self, mock_run):
        with patch.dict(os.environ, {"PORT": "9000"}, clear=True):
            main()

        mock_run.assert_called_once_with(
            "asof.server:app",
            host="0.0.0.0",
            port=9000,
        )

    @patch("asof.run_server.uvicorn.run")
    def test_rejects_an_invalid_port(self, mock_run):
        with patch.dict(os.environ, {"PORT": "70000"}, clear=True):
            with self.assertRaisesRegex(ValueError, "PORT must be between"):
                main()

        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
