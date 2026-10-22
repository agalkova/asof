import unittest

from asof import RevisionHistory, VintageValue


class ModelTests(unittest.TestCase):
    def test_revision_history_requires_sorted_unique_vintages(self):
        with self.assertRaisesRegex(ValueError, "ascending order"):
            RevisionHistory(
                series_id="PAYEMS",
                observation_date="2008-08-01",
                revisions=(
                    VintageValue("2008-10-03", 137477.0),
                    VintageValue("2008-09-05", 137473.0),
                ),
            )

    def test_models_require_iso_dates(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            VintageValue("2008-9-5", 137473.0)

    def test_vintage_values_must_be_finite(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            VintageValue("2008-09-05", float("nan"))


if __name__ == "__main__":
    unittest.main()
