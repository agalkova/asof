import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentConfigTests(unittest.TestCase):
    def test_image_does_not_copy_local_secrets(self):
        ignored = (ROOT / ".dockerignore").read_text().splitlines()

        self.assertIn(".env*", ignored)
        self.assertIn(".git", ignored)
        self.assertIn("data", ignored)

    def test_container_runs_as_an_unprivileged_user(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("useradd --create-home --uid 10001 asof", dockerfile)
        self.assertIn("USER asof", dockerfile)
        self.assertIn('CMD ["python", "-m", "asof.run_server"]', dockerfile)

    def test_legacy_render_blueprint_is_removed(self):
        self.assertFalse((ROOT / "render.yaml").exists())

    def test_ci_runs_the_test_suite_without_cloud_permissions(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text()

        self.assertIn("contents: read", workflow)
        self.assertIn("python -m unittest discover -s tests -v", workflow)
        self.assertNotIn("id-token: write", workflow)


if __name__ == "__main__":
    unittest.main()
