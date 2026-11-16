"""Archived from tests/test_telemetry_shadow_promotion.py (trendhold sleeve retired 2026-11-15).
Not collected by pytest (testpaths=tests); kept for reference only."""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd
from dublin_bot.scorecard import write_scorecard
from .test_d1_trendhold import _th, _world
from .test_meanrev_sleeve import NOW, TF, FakeGateway, _tick


# ── sleeves: shadow candidates, decisions and rich closed trades ──
def test_trendhold_blocked_entry_becomes_shadow_candidate_and_decision_row():
    bars, ticks = _world()
    Path("logs").mkdir(exist_ok=True)
    Path("logs/paper_bot_positions.json").write_text(json.dumps({"ETH/USD": 0.01, "SOL/USD": 0.01}))
    res = _th(FakeGateway(bars, ticks), max_concurrent_positions=2).run_cycle()
    assert res.blocked and res.blocked[0]["gate"] == "max_positions"
    assert res.blocked[0]["strategy"] == "trendhold@240m" and res.blocked[0]["signal_bar_open"]
    rows = [json.loads(x) for x in Path("logs/decision_snapshots.jsonl").read_text().splitlines()]
    btc = [r for r in rows if r["symbol"] == "BTC/USD" and r["strategy"] == "trendhold@240m"]
    assert btc and btc[-1]["action"] == "BUY" and btc[-1]["d1"]["riskon"] is True
    assert btc[-1]["indicators"].get("close") and btc[-1]["learner"]["min_sample"] >= 30


def test_trendhold_d1_block_is_a_shadow_candidate():
    bars, ticks = _world()
    res = _th(FakeGateway(bars, ticks), rising=False).run_cycle()
    assert [b["gate"] for b in res.blocked if b["symbol"] == "BTC/USD"] == ["d1"]


def test_trendhold_exit_writes_rich_closed_trade():
    bars, ticks = _world()
    _th(FakeGateway(bars, ticks)).run_cycle()
    later = NOW + 2 * TF
    bars2 = {s: b.set_axis(b.index + pd.Timedelta(seconds=2 * TF)) for s, b in _world("break")[0].items()}
    ticks2 = {s: _tick(float(b["close"].iloc[-1])) for s, b in bars2.items()}
    _th(FakeGateway(bars2, ticks2), now=later).run_cycle()
    row = json.loads(Path("logs/closed_trades.jsonl").read_text().strip().splitlines()[-1])
    assert row["sleeve"] == "trendhold@240m" and row["exit_reason"] == "below_ema100"
    for k in ("entry_signal_px", "entry_fill_px", "exit_signal_px", "exit_fill_px", "fees_usd",
              "entry_maker", "exit_maker", "mae_bps", "mfe_bps", "hold_s", "net_bps", "trade_id"):
        assert row.get(k) is not None, k
    assert row["hold_s"] == 2 * TF and row["mae_bps"] < 0 and row["mae_bps"] <= row["mfe_bps"]
    card = write_scorecard(Path("logs"))
    det = card["sleeves"]["trendhold@240m"]["detail"]
    assert card["sleeves"]["trendhold@240m"]["trades"] == 1  # rich row + learner row deduplicated
    assert det["rich_trades"] == 1 and det["exit_reasons"] == {"below_ema100": 1}
    assert "shadow" in card and "equity" in card and "promotion" in card


