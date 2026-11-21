"""Put credit spread sleeve: math, selection, sizing, exits, ledger and locks."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from dublin_bot.audit import AuditLog
from dublin_bot.config import Settings
from dublin_bot.credit_spread import (
    ET,
    CreditSpreadSleeve,
    OpenSpread,
    SpreadBook,
    build_candidate,
    close_fees,
    close_order_body,
    exit_decision,
    normalize_iv_rank,
    open_fees,
    open_order_body,
    pick_expiration,
    regular_session_open,
    screen_underlying,
    size_contracts,
)
from dublin_bot.options_math import (
    bs_put,
    bs_put_delta,
    implied_vol_put,
    occ_symbol,
    parse_occ,
)
from dublin_bot.tasty_backtest import Variant, score_trials, split_verdict

ACK = "I_ACCEPT_LIVE_TRADING_RISK"
TODAY = date(2026, 11, 23)  # a Monday
NOW = datetime(2026, 11, 23, 11, 0, tzinfo=ET)


# ── option maths ────────────────────────────────────────────
def test_occ_round_trip():
    sym = occ_symbol("SPY", date(2026, 12, 31), "P", 587.5)
    assert sym == "SPY   261231P00587500"
    p = parse_occ(sym)
    assert (p.root, p.expiration, p.right, p.strike) == ("SPY", date(2026, 12, 31), "P", 587.5)


def test_implied_vol_inverts_black_scholes():
    px = bs_put(600, 570, 45 / 365, 0.19)
    assert implied_vol_put(px, 600, 570, 45 / 365) == pytest.approx(0.19, abs=1e-4)
    assert implied_vol_put(0.0, 600, 570, 0.1) is None
    assert -0.5 < bs_put_delta(600, 570, 45 / 365, 0.19) < -0.1


def test_iv_rank_normalisation():
    assert normalize_iv_rank("0.42") == 0.42
    assert normalize_iv_rank(42.0) == 0.42
    assert normalize_iv_rank(None) is None


# ── synthetic but internally consistent chain ───────────────
SPOT = 600.0


def _vol(k: float) -> float:  # downside skew like real index options
    return 0.19 + 0.6 * max(0.0, 1 - k / SPOT)  # elevated-IV regime (IV rank >= 30)


def make_chain(dtes=(21, 45, 74)):
    exps = []
    for d in dtes:
        exp = TODAY + timedelta(days=d)
        strikes = [{"strike-price": str(k), "put": occ_symbol("SPY", exp, "P", k),
                    "call": occ_symbol("SPY", exp, "C", k)} for k in range(450, 601)]
        exps.append({"expiration-date": exp.isoformat(), "days-to-expiration": d,
                     "expiration-type": "Regular", "strikes": strikes})
    return {"underlying-symbol": "SPY", "expirations": exps}


def quote_for(sym: str, today=TODAY, spot=SPOT, vol_shift=0.0, width_frac=0.03):
    p = parse_occ(sym)
    t = max((p.expiration - today).days, 1) / 365
    px = bs_put(spot, p.strike, t, _vol(p.strike) + vol_shift)
    half = max(0.01, px * width_frac / 2)
    return {"bid": f"{max(px - half, 0.0):.2f}", "ask": f"{px + half:.2f}"}


def chain_quotes(exp, **kw):
    return {k["put"]: quote_for(k["put"], **kw) for k in exp["strikes"]}


def S(tmp_path, **kw) -> Settings:
    base = dict(audit_log_path=tmp_path / "a.jsonl", options_ledger_path=tmp_path / "spreads.json",
                options_sleeve_enabled=True, options_underlyings=["SPY"])
    base.update(kw)
    return Settings(**base)


def test_expiration_closest_to_target_inside_window(tmp_path):
    exp = pick_expiration(make_chain(), S(tmp_path))
    assert exp["days-to-expiration"] == 45
    assert pick_expiration(make_chain(dtes=(10, 90)), S(tmp_path)) is None


def test_candidate_targets_delta_and_respects_credit_floor(tmp_path):
    s = S(tmp_path)
    exp = pick_expiration(make_chain(), s)
    c, why = build_candidate("SPY", exp, SPOT, chain_quotes(exp), s, TODAY)
    assert c is not None, why
    assert abs(c.short_delta - 0.20) <= 0.02
    assert c.width in s.options_width_candidates
    assert c.long_strike == c.short_strike - c.width
    assert c.credit_ratio >= s.options_min_credit_ratio
    assert c.credit_natural <= c.limit_credit <= c.credit_mid + 1e-9


def test_wide_markets_are_rejected(tmp_path):
    s = S(tmp_path)
    exp = pick_expiration(make_chain(), s)
    c, why = build_candidate("SPY", exp, SPOT, chain_quotes(exp, width_frac=0.5), s, TODAY)
    assert c is None and "tight market" in why


def test_low_premium_fails_credit_ratio(tmp_path):
    s = S(tmp_path, options_min_credit_ratio=0.45)
    exp = pick_expiration(make_chain(), s)
    c, why = build_candidate("SPY", exp, SPOT, chain_quotes(exp), s, TODAY)
    assert c is None and "of width" in why


def test_screen_iv_rank_liquidity_and_earnings(tmp_path):
    s = S(tmp_path)
    assert screen_underlying("SPY", {"implied-volatility-index-rank": "0.35",
                                     "liquidity-rating": 4}, s)[0]
    assert not screen_underlying("SPY", {"implied-volatility-index-rank": "0.10"}, s)[0]
    assert not screen_underlying("SPY", {"implied-volatility-index-rank": "0.5",
                                         "liquidity-rating": 1}, s)[0]
    ok, why = screen_underlying("AAPL", {"implied-volatility-index-rank": "0.5",
                                         "earnings": {"expected-report-date": "2026-12-01"}},
                                s, date(2026, 12, 19))
    assert not ok and "earnings" in why


def test_sizing_caps_per_trade_and_total_risk(tmp_path):
    s = S(tmp_path)
    exp = pick_expiration(make_chain(), s)
    c, _ = build_candidate("SPY", exp, SPOT, chain_quotes(exp), s, TODAY, max_width=5)
    per = c.max_loss_per_spread + open_fees(1, s) + close_fees(1, s)
    assert size_contracts(c, 40_000, 0, s) == int(2_000 // per)
    assert size_contracts(c, 40_000, 9_600, s) == int(400 // per)  # near the 25% total cap
    assert size_contracts(c, 1_000, 0, s) == 0


def test_fees_cap_commission_per_leg(tmp_path):
    s = S(tmp_path)
    assert open_fees(1, s) == 2.20
    assert open_fees(20, s) == 24.0  # $10 cap x 2 legs + clearing
    assert close_fees(3, s) == 0.60


def _spread(**kw):
    base = dict(id="x", underlying="SPY", expiration=(TODAY + timedelta(days=45)).isoformat(),
                short_symbol="S", long_symbol="L", short_strike=570, long_strike=565, width=5,
                contracts=1, entry_credit=1.00, entry_time=NOW.isoformat(), entry_fees=2.2,
                max_loss=402.2, short_delta=0.2)
    base.update(kw)
    return OpenSpread(**base)


def test_exit_rules_order(tmp_path):
    s = S(tmp_path)
    sp = _spread()
    assert exit_decision(sp, 0.70, TODAY, s) is None
    assert exit_decision(sp, 0.50, TODAY, s) == "take_profit"
    assert exit_decision(sp, 3.00, TODAY, s) == "stop_loss"
    assert exit_decision(sp, 0.9, TODAY + timedelta(days=24), s) == "time_exit"
    assert exit_decision(sp, 0.9, TODAY + timedelta(days=45), s) == "expiration"


def test_order_bodies_match_tastytrade_spread_format(tmp_path):
    s = S(tmp_path)
    exp = pick_expiration(make_chain(), s)
    c, _ = build_candidate("SPY", exp, SPOT, chain_quotes(exp), s, TODAY)
    ob = open_order_body(c, 2, "mayo-o-1")
    assert ob["price-effect"] == "Credit" and ob["order-type"] == "Limit"
    assert [leg["action"] for leg in ob["legs"]] == ["Sell to Open", "Buy to Open"]
    assert all(leg["instrument-type"] == "Equity Option" and leg["quantity"] == "2"
               for leg in ob["legs"])
    cb = close_order_body(_spread(contracts=2), 0.503, "mayo-x-1")
    assert cb["price"] == "0.51" and cb["price-effect"] == "Debit"
    assert [leg["action"] for leg in cb["legs"]] == ["Buy to Close", "Sell to Close"]


def test_session_window():
    assert regular_session_open(NOW)
    assert not regular_session_open(datetime(2026, 11, 23, 9, 35, tzinfo=ET))
    assert not regular_session_open(datetime(2026, 11, 22, 12, 0, tzinfo=ET))  # Sunday


# ── full sleeve cycle against a fake broker ─────────────────
class FakeBroker:
    is_production = False
    has_credentials = True

    def __init__(self, ivr="0.40"):
        self.ivr = ivr
        self.today = TODAY
        self.spot = SPOT
        self.vol_shift = 0.0
        self.chain = make_chain()
        self.submits, self.dry_runs = [], []

    def market_metrics(self, syms):
        return {s: {"implied-volatility-index-rank": self.ivr, "liquidity-rating": 4} for s in syms}

    def nested_option_chain(self, sym):
        return self.chain

    def market_data(self, equity=(), equity_option=(), cryptocurrency=()):
        out = {e: {"mid": str(self.spot)} for e in equity}
        for o in equity_option:
            out[o] = quote_for(o, today=self.today, spot=self.spot, vol_shift=self.vol_shift)
        return out

    def positions(self, *a, **k):
        return []

    def dry_run_order(self, acct, body):
        self.dry_runs.append(body)
        return {"errors": []}

    def submit_order(self, acct, body):
        self.submits.append(body)
        return {"order": {"id": 1}}


def sleeve(tmp_path, broker, **kw):
    s = S(tmp_path, **kw)
    return CreditSpreadSleeve(s, client=broker, audit=AuditLog(s.audit_log_path),
                              book=SpreadBook(s.options_ledger_path)), s


def test_cycle_enters_one_paper_spread_and_persists(tmp_path):
    b = FakeBroker()
    sl, s = sleeve(tmp_path, b)
    res = sl.run_cycle(NOW)
    entry = res["entries"][0]
    assert entry["mode"] == "paper" and entry["contracts"] >= 1
    assert entry["max_loss"] <= s.options_equity_usd * s.options_risk_per_trade
    assert b.submits == []
    again = SpreadBook(s.options_ledger_path)
    assert len(again.open) == 1
    assert sl.run_cycle(NOW)["entries"][0]["skip"] == "already holding a spread"


def test_budget_steps_down_from_ten_wide(tmp_path):
    sl, _ = sleeve(tmp_path, FakeBroker(), options_equity_usd=10_000)
    entry = sl.run_cycle(NOW)["entries"][0]
    assert entry["width"] == 5.0  # 10-wide max loss exceeds the 5% budget


def test_narrow_spreads_rejected_for_fee_drag(tmp_path):
    s = S(tmp_path, options_width_candidates=[2.0, 1.0])
    exp = pick_expiration(make_chain(), s)
    c, why = build_candidate("SPY", exp, SPOT, chain_quotes(exp), s, TODAY)
    assert c is None and "costs" in why


def test_small_account_cannot_trade_spreads(tmp_path):
    sl, _ = sleeve(tmp_path, FakeBroker(), options_equity_usd=2_000)
    assert "risk budget too small" in sl.run_cycle(NOW)["entries"][0]["skip"]


def test_budget_too_small_for_any_width(tmp_path):
    sl, _ = sleeve(tmp_path, FakeBroker(), options_equity_usd=200,
                   options_risk_per_trade=0.01, options_max_total_risk=0.02)
    entry = sl.run_cycle(NOW)["entries"][0]
    assert "risk budget too small" in entry["skip"]


def test_low_iv_rank_means_no_trade(tmp_path):
    sl, _ = sleeve(tmp_path, FakeBroker(ivr="0.12"))
    assert "IV rank" in sl.run_cycle(NOW)["entries"][0]["skip"]


def test_rally_triggers_take_profit_with_fees(tmp_path):
    b = FakeBroker()
    sl, s = sleeve(tmp_path, b, options_equity_usd=10_000)
    sl.run_cycle(NOW)
    b.today, b.spot = TODAY + timedelta(days=7), SPOT * 1.04
    later = NOW + timedelta(days=7)
    exits = sl.run_cycle(later)["exits"]
    assert exits[0]["exit"] == "take_profit" and exits[0]["pnl"] > 0
    sp = sl.book.spreads[0]
    assert sp.realized_pnl == pytest.approx(
        (sp.entry_credit - sp.exit_debit) * 100 * sp.contracts - sp.entry_fees - sp.exit_fees)


def test_crash_triggers_stop_and_loss_is_bounded(tmp_path):
    b = FakeBroker()
    sl, s = sleeve(tmp_path, b, options_equity_usd=10_000)
    sl.run_cycle(NOW)
    b.today, b.spot, b.vol_shift = TODAY + timedelta(days=3), SPOT * 0.93, 0.15
    exits = sl.run_cycle(NOW + timedelta(days=3))["exits"]
    sp = sl.book.spreads[0]
    assert exits[0]["exit"] == "stop_loss"
    assert sp.realized_pnl < 0 and -sp.realized_pnl <= sp.max_loss + 1e-6


def test_outside_hours_and_disabled_do_nothing(tmp_path):
    sl, _ = sleeve(tmp_path, FakeBroker())
    assert "outside" in sl.run_cycle(datetime(2026, 11, 23, 8, 0, tzinfo=ET))["skipped"]
    sl2, _ = sleeve(tmp_path, FakeBroker(), options_sleeve_enabled=False)
    assert "OPTIONS_SLEEVE_ENABLED" in sl2.run_cycle(NOW)["skipped"]


def test_live_requires_options_lock_and_production(tmp_path):
    b = FakeBroker()
    live = dict(paper_trading=False, dry_run=False, allow_live_trading=True,
                live_risk_acknowledgement=ACK, tastytrade_account_number="5WT1")
    s = S(tmp_path, **live)
    sl = CreditSpreadSleeve(s, client=b, audit=AuditLog(s.audit_log_path),
                            allow_order_submission=True)
    assert sl.live_enabled is False  # options venue lock still closed
    s2 = S(tmp_path, allow_options_live_orders=True, **live)
    sl2 = CreditSpreadSleeve(s2, client=b, audit=AuditLog(s2.audit_log_path),
                             allow_order_submission=True)
    assert sl2.live_enabled is False  # sandbox host is not production
    b.is_production = True
    assert sl2.live_enabled is True
    res = sl2.run_cycle(NOW)
    assert res["entries"][0]["mode"] == "live"
    assert len(b.dry_runs) == 1 and len(b.submits) == 1
    assert SpreadBook(s2.options_ledger_path).open[0].status == "pending_open"


def test_config_rejects_inconsistent_dte(tmp_path):
    with pytest.raises(ValueError):
        S(tmp_path, options_exit_dte=40)


# ── backtest scoring ────────────────────────────────────────
def test_variant_body_shape():
    body = Variant("v", 20, 10, 45, 50, 200, 21, min_vix=18).body("SPY", "2018-01-01", "2026-01-01")
    assert body["legs"][0]["direction"] == "short" and body["legs"][0]["delta"] == 20
    assert body["exitConditions"] == {"takeProfitPercentage": 50, "stopLossPercentage": 200,
                                      "atDaysToExpiration": 21}
    assert body["entryConditions"]["minimumVIX"] == 18


def test_scoring_subtracts_fees_and_gates_promotion():
    win = [{"openDateTime": f"2020-01-{i % 28 + 1:02d}T{i:04d}", "profitLoss": 60} for i in range(90)]
    loss = [{"openDateTime": f"2020-02-{i % 28 + 1:02d}T{i:04d}", "profitLoss": -150} for i in range(10)]
    sc = score_trials(win + loss, fee_per_trade=6.4)
    assert sc["trades"] == 100 and sc["expectancy"] == pytest.approx((90 * 53.6 - 10 * 156.4) / 100)
    good = split_verdict(sorted(win + loss, key=lambda t: t["openDateTime"]), 6.4)
    assert "promote" in good
    bad = split_verdict([{"openDateTime": str(i), "profitLoss": 5} for i in range(100)], 6.4)
    assert bad["promote"] is False  # fees turn a thin edge negative


def test_run_validation_end_to_end_with_fake_backtester(tmp_path):
    from dublin_bot.tasty_backtest import format_report, run_validation

    class FakeBT:
        def __init__(self):
            self.bodies = []

        def run(self, body):
            self.bodies.append(body)
            trials = [{"openDateTime": f"2019-{i:04d}", "profitLoss": 45 if i % 10 else -160}
                      for i in range(200)]
            return {"status": "completed", "trials": trials, "notices": []}

    fake = FakeBT()
    rep = run_validation(S(tmp_path), symbols=["SPY"], client=object(), backtester=fake,
                         out_dir=tmp_path / "reports", end="2026-01-01")
    assert len(fake.bodies) == len(rep["results"]) >= 4
    assert rep["fee_and_slippage_per_spread"] == 6.4
    assert all(r["promote"] for r in rep["results"])  # 90% x $38.6 vs 10% x $166.4
    assert list((tmp_path / "reports").glob("credit_spread_backtest_*.json"))
    assert "promote" in format_report(rep)
