"""Deterministic offline tests; no exchange credentials, network or trade writes."""
import unittest

from scripts.research_limit_fill_audit import evaluate


def signal(**changes):
    value = {
        "signal_id": "locked-001", "venue": "binance_us", "symbol": "VETUSD",
        "close_ms": "1000", "close_price": "100", "expires_ms": "5000",
    }
    value.update(changes)
    return value


def trade(price, when, **changes):
    value = {
        "venue": "binance_us", "symbol": "VETUSD",
        "trade_ms": str(when), "price": str(price),
    }
    value.update(changes)
    return value


def full_coverage(**changes):
    value = {
        "venue": "binance_us", "symbol": "VETUSD",
        "start_ms": "1000", "end_ms": "5000", "gap_count": "0",
    }
    value.update(changes)
    return value


def audit(signals=None, prints=None, coverage=None, **kwargs):
    return evaluate(
        [signal()] if signals is None else signals,
        [] if prints is None else prints,
        [full_coverage()] if coverage is None else coverage,
        venue=kwargs.get("venue", "binance_us"),
        quote=kwargs.get("quote", "USD"),
    )


class LimitFillAuditTests(unittest.TestCase):
    def test_freezes_half_percent_below_close(self):
        record = audit()[0]
        self.assertEqual(record["limit_price"], "99.500")
        self.assertEqual(record["status"], "no_touch")
        self.assertFalse(record["confirmed_fill"])

    def test_no_lookahead_same_candle_or_after_expiry(self):
        rows = [trade("90", 1000), trade("90", 5000), trade("90", 5001)]
        self.assertEqual(audit(prints=rows)[0]["status"], "no_touch")

    def test_only_touch_does_not_count_as_filled(self):
        record = audit(prints=[trade("99.5", 2000)])[0]
        self.assertEqual(record["status"], "touch_only")
        self.assertFalse(record["confirmed_fill"])

    def test_trade_through_proxy_not_confirmed_fill(self):
        record = audit(prints=[trade("99.5", 1500), trade("99.49", 2000)])[0]
        self.assertEqual(record["status"], "traded_through")
        self.assertEqual(record["observed_trade_ms"], 2000)
        self.assertFalse(record["confirmed_fill"])

    def test_trade_through_in_unordered_feed(self):
        record = audit(prints=[trade("98", 3000), trade("99", 2000)])[0]
        self.assertEqual(record["observed_trade_ms"], 2000)

    def test_missing_coverage_or_gap_is_unknown(self):
        self.assertEqual(audit(coverage=[])[0]["status"], "coverage_unknown")
        self.assertEqual(
            audit(coverage=[full_coverage(gap_count="1")],
                  prints=[trade("90", 2000)])[0]["status"],
            "coverage_unknown",
        )
        self.assertEqual(
            audit(coverage=[full_coverage(end_ms="4000")])[0]["status"],
            "coverage_unknown",
        )

    def test_rejects_extra_coins_and_mixed_venue(self):
        with self.assertRaises(ValueError):
            audit(signals=[signal(symbol="BTCUSD")])
        with self.assertRaises(ValueError):
            audit(prints=[trade("99", 2000, venue="binance_com")])

    def test_rejects_mixed_quote(self):
        with self.assertRaises(ValueError):
            audit(signals=[signal(symbol="VETUSDT")], quote="USD")

    def test_no_duplicate_signal_ids(self):
        with self.assertRaises(ValueError):
            audit(signals=[signal(), signal()])

    def test_invalid_expiry_and_price_rejected(self):
        with self.assertRaises(ValueError):
            audit(signals=[signal(expires_ms="1000")])
        with self.assertRaises(ValueError):
            audit(signals=[signal(close_price="NaN")])

    def test_fee_or_profit_is_not_inferred(self):
        record = audit(prints=[trade("80", 2000)])[0]
        self.assertNotIn("profit_factor", record)
        self.assertNotIn("pnl", record)
        self.assertFalse(record["confirmed_fill"])


if __name__ == "__main__":
    unittest.main()
