import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentConfigTests(unittest.TestCase):
    def test_image_does_not_copy_local_secrets(self):
        ignored = (ROOT / ".dockerignore").read_text().splitlines()

        self.assertIn(".env*", ignored)
        self.assertIn(".git", ignored)
        self.assertIn("data", ignored)

    def test_render_services_use_the_asof_database_variable(self):
        blueprint = (ROOT / "render.yaml").read_text()

        self.assertIn("healthCheckPath: /health", blueprint)
        self.assertIn("preDeployCommand: asof init-db", blueprint)
        self.assertIn("asof refresh --series PAYEMS --lookback-months 24", blueprint)
        self.assertIn("key: ASOF_DATABASE_URL", blueprint)
        self.assertNotIn("key: DATABASE_URL", blueprint)
        self.assertIn("key: FRED_API_KEY", blueprint)
        self.assertIn("sync: false", blueprint)


if __name__ == "__main__":
    unittest.main()
