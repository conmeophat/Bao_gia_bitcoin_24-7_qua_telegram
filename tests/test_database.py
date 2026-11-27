from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from database import Database
from telegram_bot import TelegramBot


class AlertDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "alerts.sqlite3"
        self.db = Database(str(db_path))
        self.db.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_below_alert_fires_once_and_rearms_after_price_recovers(self) -> None:
        alert = self.db.create_alert("below", Decimal("60000"))
        alert_id = alert["id"]

        self.db.record_and_evaluate(Decimal("60000"), 1000, "test")
        self.assertEqual(len(self.db.pending_notifications()), 1)
        self.assertIn("≤ 60,000 USDT", self.db.pending_notifications()[0]["message"])

        # Staying below the threshold must not produce repeated messages.
        self.db.record_and_evaluate(Decimal("59000"), 1001, "test")
        self.assertEqual(len(self.db.pending_notifications()), 1)

        # Recover above the threshold, then cross below again.
        self.db.record_and_evaluate(Decimal("60001"), 1002, "test")
        self.db.record_and_evaluate(Decimal("59999"), 1003, "test")
        self.assertEqual(len(self.db.pending_notifications()), 2)
        self.assertEqual(self.db.get_alert(alert_id)["trigger_count"], 2)

    def test_above_alert_uses_inclusive_comparison(self) -> None:
        self.db.create_alert("above", Decimal("70000"))
        self.db.record_and_evaluate(Decimal("69999"), 2000, "test")
        self.assertEqual(len(self.db.pending_notifications()), 0)

        self.db.record_and_evaluate(Decimal("70000"), 2001, "test")
        pending = self.db.pending_notifications()
        self.assertEqual(len(pending), 1)
        self.assertIn("≥ 70,000 USDT", pending[0]["message"])

    def test_out_of_order_ticker_is_ignored(self) -> None:
        self.db.create_alert("below", Decimal("60000"))
        self.db.record_and_evaluate(Decimal("61000"), 3000, "new")
        self.db.record_and_evaluate(Decimal("59000"), 2999, "stale")
        latest = self.db.last_price()
        self.assertEqual(latest["price"], "61000")
        self.assertEqual(len(self.db.pending_notifications()), 0)

    def test_delete_alert_also_deletes_its_outbox_items(self) -> None:
        alert = self.db.create_alert("below", Decimal("60000"))
        self.db.record_and_evaluate(Decimal("59900"), 4000, "test")
        self.assertEqual(len(self.db.pending_notifications()), 1)

        self.assertTrue(self.db.delete_alert(alert["id"]))
        self.assertEqual(len(self.db.pending_notifications()), 0)

    def test_batch_alerts_create_independent_dca_levels(self) -> None:
        created = self.db.create_alerts(
            "below",
            [Decimal("65000"), Decimal("64000"), Decimal("63000")],
        )
        self.assertEqual(len(created), 3)
        self.assertEqual(
            [row["threshold"] for row in self.db.list_alerts()],
            ["65000", "64000", "63000"],
        )

        self.db.record_and_evaluate(Decimal("64500"), 5100, "test")
        pending = self.db.pending_notifications()
        self.assertEqual(len(pending), 1)
        self.assertIn("65,000 USDT", pending[0]["message"])

    def test_parse_multiple_thresholds_with_spaces_and_commas(self) -> None:
        self.assertEqual(
            TelegramBot._parse_thresholds("65000 64000 63000"),
            [Decimal("65000"), Decimal("64000"), Decimal("63000")],
        )
        self.assertEqual(
            TelegramBot._parse_thresholds("65000,64000,63000"),
            [Decimal("65000"), Decimal("64000"), Decimal("63000")],
        )
        self.assertEqual(
            TelegramBot._parse_thresholds("65000;64000;63000"),
            [Decimal("65000"), Decimal("64000"), Decimal("63000")],
        )

    def test_parse_thresholds_deduplicates_and_preserves_order(self) -> None:
        self.assertEqual(
            TelegramBot._parse_thresholds("65000,64000,65000,63000"),
            [Decimal("65000"), Decimal("64000"), Decimal("63000")],
        )

    def test_parse_thresholds_rejects_more_than_100_values(self) -> None:
        raw = " ".join(str(70000 - index) for index in range(101))
        with self.assertRaises(ValueError):
            TelegramBot._parse_thresholds(raw)

    def test_paused_alert_does_not_fire(self) -> None:
        alert = self.db.create_alert("below", Decimal("60000"))
        self.db.set_alert_active(alert["id"], False)
        self.db.record_and_evaluate(Decimal("59000"), 5000, "test")
        self.assertEqual(len(self.db.pending_notifications()), 0)


if __name__ == "__main__":
    unittest.main()
