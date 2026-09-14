import unittest

from plg_core.currency.service import build_quote_currency_presentation


def quote(**overrides):
    value = {
        "currency_code": "USD", "display_currency_mode": "USD_JMD",
        "fx_rate": "160", "fx_rate_source": "BUSINESS_WORKING_RATE",
        "fx_locked_at": "2026-01-01 00:00:00", "customer_total": 18,
    }
    value.update(overrides)
    return value


class CurrencyBPresentationTests(unittest.TestCase):
    def test_dual_display_uses_decimal_conversion_and_friendly_rate(self):
        result = build_quote_currency_presentation(quote())
        self.assertEqual((result["usd_total"], result["jmd_total"], result["rate_display"]), ("US$18.00", "J$2,880.00", "US$1 = J$160.00"))
        self.assertEqual(result["rate_source_label"], "Business working rate")

    def test_manual_rate_and_jmd_mode(self):
        result = build_quote_currency_presentation(quote(display_currency_mode="JMD", fx_rate="165", fx_rate_source="MANUAL_OVERRIDE"))
        self.assertEqual((result["mode"], result["jmd_total"], result["rate_source_label"]), ("JMD", "J$2,970.00", "Manual override"))

    def test_usd_mode_still_has_locked_snapshot(self):
        result = build_quote_currency_presentation(quote(display_currency_mode="USD"))
        self.assertEqual(result["usd_total"], "US$18.00")
        self.assertIsNone(result["jmd_total"])
        self.assertEqual(result["rate"], "160")

    def test_legacy_quote_is_usd_only_without_fabricated_jmd(self):
        result = build_quote_currency_presentation(quote(currency_code=None, display_currency_mode=None, fx_rate=None, fx_rate_source=None, fx_locked_at=None))
        self.assertTrue(result["is_legacy"])
        self.assertEqual(result["usd_total"], "US$18.00")
        self.assertIsNone(result["jmd_total"])

    def test_partial_modern_snapshot_fails_closed(self):
        with self.assertRaises(ValueError):
            build_quote_currency_presentation(quote(fx_locked_at=None))

    def test_mixed_and_unsupported_snapshots_fail_closed(self):
        cases = (
            quote(currency_code=None),
            quote(display_currency_mode=None),
            quote(currency_code="CAD"),
            quote(fx_locked_at=None),
        )
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    build_quote_currency_presentation(value)

    def test_decimal_rounding_and_zero(self):
        self.assertEqual(build_quote_currency_presentation(quote(customer_total="1.005", fx_rate="1"))["jmd_total"], "J$1.01")
        self.assertEqual(build_quote_currency_presentation(quote(customer_total="0"))["jmd_total"], "J$0.00")


if __name__ == "__main__":
    unittest.main()
