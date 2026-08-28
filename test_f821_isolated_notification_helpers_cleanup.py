import unittest

import notifier
from scripts.check_f821 import KNOWN_F821_DEBT
from test_f821_orphaned_renderers_cleanup import _symbol_references


REMOVED_PRIVATE_SUBGRAPH = (
    "_booking" + "_link",
    "_append_round_trip_" + "recommendations",
    "_append_simple_" + "top3",
    "_round_trip_score_" + "line",
    "_append_round_trip_score_" + "top3",
)

PUBLIC_COMPATIBILITY_DEBT = (
    "notifier.py",
    "generate_neutral_summary",
    "_plain_price_position",
)


class IsolatedNotificationHelperCleanupTest(unittest.TestCase):
    def test_removed_private_subgraphs_have_no_static_or_dynamic_references(self):
        for symbol in REMOVED_PRIVATE_SUBGRAPH:
            with self.subTest(symbol=symbol):
                self.assertEqual(_symbol_references(symbol), [])

    def test_public_neutral_summary_stays_explicitly_registered_for_adjudication(self):
        self.assertTrue(callable(notifier.generate_neutral_summary))
        self.assertIn(PUBLIC_COMPATIBILITY_DEBT, KNOWN_F821_DEBT)
        with self.assertRaisesRegex(NameError, "_plain_price_position"):
            notifier.generate_neutral_summary(
                {"price_range": [1000, 1200]},
                {"current_position": "低位"},
            )

    def test_remaining_debt_is_only_public_compatibility_debt(self):
        from test_f821_orphaned_renderers_cleanup import MANUAL_RENDERER_DEBT

        self.assertEqual(MANUAL_RENDERER_DEBT, frozenset())
        self.assertEqual(
            KNOWN_F821_DEBT,
            {PUBLIC_COMPATIBILITY_DEBT},
        )


if __name__ == "__main__":
    unittest.main()
