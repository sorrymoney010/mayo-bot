"""Tests for the safety infrastructure: nonce, rate limiting, precision,
freshness, idempotency, and the audit hash chain."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from dublin_bot.audit import AuditEvent, AuditLog, redact
from dublin_bot.errors import DuplicateOrderError, PrecisionError, StaleDataError
from dublin_bot.freshness import FreshnessGuard, check_monotonic_bars
from dublin_bot.idempotency import (
    IdempotencyLedger,
    make_intent_key,
    userref_from_key,
)
from dublin_bot.nonce import NonceGenerator
from dublin_bot.precision import PairPrecision, round_volume, size_order
from dublin_bot.ratelimit import KrakenRateLimiter, RateLimitTier, TokenBucket


# ── nonce ───────────────────────────────────────────────────────────

def test_nonce_is_strictly_increasing(tmp_path):
    gen = NonceGenerator(tmp_path / "nonce.json")
    values = [gen.next() for _ in range(200)]
    assert values == sorted(values)
    assert len(set(values)) == 200


def test_nonce_survives_restart(tmp_path):
    """A restart must never reissue a nonce the exchange has already seen."""
    path = tmp_path / "nonce.json"
    first = NonceGenerator(path)
    high = max(first.next() for _ in range(10))
    second = NonceGenerator(path)
    assert second.next() > high


def test_nonce_recovers_from_backwards_clock(tmp_path):
    """Simulates an NTP correction or laptop sleep moving the clock back."""
    path = tmp_path / "nonce.json"
    gen = NonceGenerator(path)
    gen.next()
    # Force the persisted high-water mark far into the future.
    path.write_text(json.dumps({"last_nonce": 99_999_999_999_999_999}))
    resumed = NonceGenerator(path)
    assert resumed.next() > 99_999_999_999_999_999


def test_nonce_tolerates_corrupt_state_file(tmp_path):
    path = tmp_path / "nonce.json"
    path.write_text("{ not json")
    assert NonceGenerator(path).next() > 0


def test_nonce_is_thread_safe(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    gen = NonceGenerator(tmp_path / "nonce.json")
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _i: gen.next(), range(400)))
    assert len(set(values)) == 400


def test_independent_nonce_generators_share_one_high_water_mark(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "nonce.json"
    generators = [NonceGenerator(path) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda i: generators[i % 8].next(), range(400)))
    assert len(set(values)) == 400
    assert json.loads(path.read_text())["last_nonce"] == max(values)


# ── rate limiting ───────────────────────────────────────────────────

def test_token_bucket_blocks_when_exhausted():
    clock = {"t": 0.0}
    bucket = TokenBucket(5.0, 1.0, time_fn=lambda: clock["t"], sleep_fn=lambda s: None)
    for _ in range(5):
        assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False


def test_token_bucket_refills_over_time():
    clock = {"t": 0.0}
    bucket = TokenBucket(5.0, 1.0, time_fn=lambda: clock["t"], sleep_fn=lambda s: None)
    for _ in range(5):
        bucket.try_acquire()
    clock["t"] = 3.0
    assert bucket.tokens == pytest.approx(3.0)
    assert bucket.try_acquire(3.0) is True


def test_acquire_waits_rather_than_failing():
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    bucket = TokenBucket(2.0, 1.0, time_fn=lambda: clock["t"], sleep_fn=sleep)
    bucket.acquire(2.0)
    waited = bucket.acquire(2.0)
    assert waited == pytest.approx(2.0)


def test_acquire_times_out_instead_of_hanging():
    bucket = TokenBucket(1.0, 0.01, time_fn=lambda: 0.0, sleep_fn=lambda s: None)
    bucket.acquire(1.0)
    with pytest.raises(TimeoutError):
        bucket.acquire(1.0, timeout=5.0)


def test_kraken_limiter_costs_differ_per_endpoint():
    limiter = KrakenRateLimiter(RateLimitTier.starter())
    assert limiter.private_cost("TradeBalance") > limiter.private_cost("Balance")
    snapshot = limiter.snapshot()
    assert snapshot["private_capacity"] == 15.0


# ── precision ───────────────────────────────────────────────────────

BTC = PairPrecision("XXBTZUSD", lot_decimals=8, pair_decimals=1,
                    order_min=Decimal("0.00005"), cost_min=Decimal("0.5"))


def test_volume_always_rounds_down():
    """Rounding up could breach the position cap; rounding down cannot."""
    assert round_volume(0.123456789, BTC) == Decimal("0.12345678")
    assert round_volume(0.999999999, BTC) == Decimal("0.99999999")


def test_size_order_produces_valid_volume():
    order = size_order(100.0, 50_000.0, BTC)
    assert order.volume == Decimal("0.00200000")
    assert order.notional == pytest.approx(Decimal("100.0"))


def test_size_order_rejects_below_order_min():
    with pytest.raises(PrecisionError, match="below Kraken minimum"):
        size_order(1.0, 50_000.0, BTC)


def test_size_order_rejects_below_cost_min():
    tiny = PairPrecision("TESTUSD", lot_decimals=8, pair_decimals=2,
                         order_min=Decimal("0"), cost_min=Decimal("10"))
    with pytest.raises(PrecisionError, match="below Kraken minimum cost"):
        size_order(5.0, 100.0, tiny)


def test_size_order_rejects_below_configured_floor():
    with pytest.raises(PrecisionError, match="below configured minimum"):
        size_order(10.0, 50_000.0, BTC, min_notional_usd=25.0)


def test_size_order_rejects_dust_that_rounds_to_zero():
    coarse = PairPrecision("COARSE", lot_decimals=2, pair_decimals=2,
                           order_min=Decimal("0"))
    with pytest.raises(PrecisionError, match="rounds to zero"):
        size_order(0.001, 50_000.0, coarse)


def test_volume_string_never_uses_scientific_notation():
    """Kraken rejects '1e-05'; the wire format must be plain decimal."""
    order = size_order(2.5, 50_000.0, BTC)
    assert "e" not in order.volume_str.lower()
    assert order.volume_str.startswith("0.0000")


def test_pair_precision_parses_kraken_metadata():
    precision = PairPrecision.from_kraken("XXBTZUSD", {
        "lot_decimals": 8, "pair_decimals": 1,
        "ordermin": "0.00005", "costmin": "0.5",
    })
    assert precision.order_min == Decimal("0.00005")


# ── freshness ───────────────────────────────────────────────────────

def _bars_ending(minutes_ago: float, periods: int = 10) -> pd.DataFrame:
    end = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    index = pd.date_range(end=end, periods=periods, freq="1h", tz="UTC")
    return pd.DataFrame({"close": [1.0] * periods}, index=index)


def test_fresh_data_passes():
    guard = FreshnessGuard(60)
    assert guard.evaluate_bars(_bars_ending(30)).fresh is True


def test_stale_data_is_rejected():
    guard = FreshnessGuard(60)
    verdict = guard.evaluate_bars(_bars_ending(60 * 24 * 21))
    assert verdict.fresh is False
    assert "stale" in verdict.reason.lower()
    assert verdict.bar_age_minutes > 1000


def test_boundary_is_three_intervals():
    guard = FreshnessGuard(60, max_bar_age_multiple=3.0)
    assert guard.evaluate_bars(_bars_ending(179)).fresh is True
    assert guard.evaluate_bars(_bars_ending(181)).fresh is False


def test_future_bars_are_rejected():
    guard = FreshnessGuard(60)
    verdict = guard.evaluate_bars(_bars_ending(-300))
    assert verdict.fresh is False
    assert "future" in verdict.reason.lower()


def test_clock_skew_is_rejected():
    guard = FreshnessGuard(60, max_clock_skew_seconds=30)
    now = datetime.now(timezone.utc)
    verdict = guard.evaluate_bars(
        _bars_ending(10), now=now, server_time=now.timestamp() + 600
    )
    assert verdict.fresh is False
    assert "skew" in verdict.reason.lower()


def test_small_clock_skew_is_tolerated():
    guard = FreshnessGuard(60, max_clock_skew_seconds=30)
    now = datetime.now(timezone.utc)
    verdict = guard.evaluate_bars(
        _bars_ending(10), now=now, server_time=now.timestamp() + 5
    )
    assert verdict.fresh is True


def test_empty_bars_are_rejected():
    guard = FreshnessGuard(60)
    assert guard.evaluate_bars(pd.DataFrame()).fresh is False


def test_verdict_raises_when_stale():
    guard = FreshnessGuard(60)
    with pytest.raises(StaleDataError):
        guard.evaluate_bars(_bars_ending(100_000)).raise_if_stale()


def test_duplicate_and_unordered_bars_are_faults():
    """Duplicated candles silently corrupt every rolling indicator."""
    index = pd.DatetimeIndex(["2026-01-01T00:00Z", "2026-01-01T00:00Z"])
    with pytest.raises(StaleDataError, match="duplicate"):
        check_monotonic_bars(pd.DataFrame({"close": [1.0, 2.0]}, index=index))

    reversed_index = pd.DatetimeIndex(["2026-01-01T02:00Z", "2026-01-01T01:00Z"])
    with pytest.raises(StaleDataError, match="monotonic"):
        check_monotonic_bars(pd.DataFrame({"close": [1.0, 2.0]}, index=reversed_index))


# ── idempotency ─────────────────────────────────────────────────────

def test_intent_key_is_deterministic():
    args = dict(symbol="BTC/USD", side="buy", notional_usd=6.25,
                bar_timestamp="2026-08-04T12:00:00Z")
    assert make_intent_key(**args) == make_intent_key(**args)


def test_intent_key_changes_with_the_bar():
    base = dict(symbol="BTC/USD", side="buy", notional_usd=6.25)
    a = make_intent_key(**base, bar_timestamp="2026-08-04T12:00:00Z")
    b = make_intent_key(**base, bar_timestamp="2026-08-04T13:00:00Z")
    assert a != b


def test_userref_fits_kraken_32bit_range():
    for i in range(200):
        ref = userref_from_key(f"key-{i}")
        assert 0 < ref <= 2_147_483_647


def _reserve(ledger, key="k1", **overrides):
    args = dict(key=key, symbol="BTC/USD", side="buy", notional_usd=6.25,
                bar_timestamp="2026-08-04T12:00:00Z", dry_run=True)
    args.update(overrides)
    return ledger.reserve(**args)


def test_duplicate_reservation_is_blocked(tmp_path):
    ledger = IdempotencyLedger(tmp_path / "orders.json")
    _reserve(ledger)
    with pytest.raises(DuplicateOrderError):
        _reserve(ledger)


def test_confirmed_order_cannot_be_resubmitted(tmp_path):
    ledger = IdempotencyLedger(tmp_path / "orders.json")
    _reserve(ledger)
    ledger.confirm("k1", "OABC-1")
    with pytest.raises(DuplicateOrderError, match="confirmed"):
        _reserve(ledger)


def test_failed_order_may_be_retried(tmp_path):
    """A definitively failed order never reached the book, so retry is safe."""
    ledger = IdempotencyLedger(tmp_path / "orders.json")
    _reserve(ledger)
    ledger.fail("k1", "insufficient funds")
    record = _reserve(ledger)
    assert record.status == "pending"


def test_ledger_survives_restart(tmp_path):
    path = tmp_path / "orders.json"
    ledger = IdempotencyLedger(path)
    _reserve(ledger)
    ledger.confirm("k1", "OABC-1")

    reloaded = IdempotencyLedger(path)
    assert reloaded.get("k1").order_id == "OABC-1"
    with pytest.raises(DuplicateOrderError):
        _reserve(reloaded)


def test_pending_intents_are_visible_after_crash(tmp_path):
    """The 'we don't know if it landed' case must be recoverable."""
    path = tmp_path / "orders.json"
    ledger = IdempotencyLedger(path)
    _reserve(ledger, dry_run=False)
    reloaded = IdempotencyLedger(path)
    assert len(reloaded.pending()) == 1


def test_prune_keeps_pending_and_drops_old_settled(tmp_path):
    ledger = IdempotencyLedger(tmp_path / "orders.json", retention_days=1)
    _reserve(ledger, key="old")
    ledger.confirm("old", "O1")
    ledger.get("old").updated_at = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).isoformat()
    _reserve(ledger, key="live", dry_run=False)

    assert ledger.prune() == 1
    assert ledger.get("old") is None
    assert ledger.get("live") is not None


# ── audit log ───────────────────────────────────────────────────────

def test_audit_appends_and_chains(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record(AuditEvent.STARTUP, {"a": 1})
    log.record(AuditEvent.SIGNAL, {"action": "BUY"})
    intact, reason = log.verify_chain()
    assert intact is True
    assert "2 entries" in reason


def test_audit_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record(AuditEvent.SIGNAL, {"action": "BUY"})
    log.record(AuditEvent.SIGNAL, {"action": "SELL"})

    lines = path.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["payload"]["action"] = "SELL"      # rewrite history
    lines[0] = json.dumps(entry, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")

    intact, reason = AuditLog(path).verify_chain()
    assert intact is False
    assert "tampered" in reason


def test_audit_detects_deleted_entry(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.record(AuditEvent.SIGNAL, {"i": i})
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")

    intact, reason = AuditLog(path).verify_chain()
    assert intact is False
    assert "chain break" in reason


def test_audit_chain_continues_across_restart(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record(AuditEvent.STARTUP, {})
    AuditLog(path).record(AuditEvent.SHUTDOWN, {})
    assert AuditLog(path).verify_chain()[0] is True


def test_audit_chain_survives_concurrent_independent_writers(tmp_path):
    """Dashboard threads create separate AuditLog objects for one file."""
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "audit.jsonl"

    def write(index: int) -> None:
        AuditLog(path).record(AuditEvent.SIGNAL, {"index": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(50)))

    intact, reason = AuditLog(path).verify_chain()
    assert intact is True, reason
    assert len(AuditLog(path).tail(100)) == 50


def test_secrets_are_redacted():
    payload = {
        "api_key": "abc123",
        "kraken_api_secret": "supersecret",
        "nested": {"password": "hunter2", "safe": "visible"},
        "list": [{"token": "t"}],
        "notional": 6.25,
    }
    cleaned = redact(payload)
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["kraken_api_secret"] == "[REDACTED]"
    assert cleaned["nested"]["password"] == "[REDACTED]"
    assert cleaned["nested"]["safe"] == "visible"
    assert cleaned["list"][0]["token"] == "[REDACTED]"
    assert cleaned["notional"] == 6.25


def test_audit_never_writes_a_secret_to_disk(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).record(AuditEvent.CONFIG_SNAPSHOT,
                          {"kraken_api_secret": "TOPSECRETVALUE"})
    assert "TOPSECRETVALUE" not in path.read_text()
    assert "[REDACTED]" in path.read_text()
