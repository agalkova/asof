import unittest

from asof import AsOfLookup, RevisionHistory, VintageValue


class ModelTests(unittest.TestCase):
    def test_revision_history_rejects_unsafe_series_ids(self):
        with self.assertRaisesRegex(ValueError, "series_id"):
            RevisionHistory(
                series_id="PAYEMS/../../other",
                observation_date="2008-08-01",
                revisions=(VintageValue("2008-09-05", 137473.0),),
            )

    def test_missing_observation_cannot_include_a_revision(self):
        with self.assertRaisesRegex(ValueError, "missing observation"):
            AsOfLookup(
                observation_exists=False,
                revision=VintageValue("2008-09-05", 137473.0),
            )

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

    def test_revision_history_returns_the_value_known_as_of_a_date(self):
        history = RevisionHistory(
            series_id="PAYEMS",
            observation_date="2008-08-01",
            revisions=(
                VintageValue("2008-09-05", 137473.0),
                VintageValue("2008-10-03", 137477.0),
            ),
        )

        before_release = history.value_as_of("2008-09-04")
        first_release = history.value_as_of("2008-09-12")
        revised = history.value_as_of("2008-10-03")

        self.assertIsNone(before_release)
        self.assertEqual(first_release, VintageValue("2008-09-05", 137473.0))
        self.assertEqual(revised, VintageValue("2008-10-03", 137477.0))


if __name__ == "__main__":
    unittest.main()
