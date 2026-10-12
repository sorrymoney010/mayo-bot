import json
import numpy as np
import pandas as pd
from dublin_bot.alt_regime_shadow import AltRegimeShadow, build_alt_shadow


def _fake(sym, tf):
    n = 400
    t = (np.arange(n) * tf * 60 + 1_700_000_000 // (tf * 60) * tf * 60).astype("int64")
    c = 100 * np.exp(np.linspace(0, 0.6, n))  # steady uptrend -> breakout/ADX entry
    df = pd.DataFrame({"time": t, "open": c * 0.999, "high": c * 1.003, "low": c * 0.997,
                       "close": c, "volume": 1.0})
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    return df


def test_shadow_writes_virtual_ledger_only(tmp_path):
    s = AltRegimeShadow(symbols=["XRP/USD", "ADA/USD", "LTC/USD", "DOT/USD"], logs_dir=tmp_path,
                        max_open=3, fetch=_fake)
    last_open = int(_fake("x", 240)["time"].iloc[-1])
    now = last_open + 240 * 60 * 2 + 100  # last fake bar is the last closed one
    s.fetch = lambda sym, tf: _fake(sym, tf)
    s._bar_open = lambda _now: last_open
    r = s.run_cycle(now)
    st = json.loads((tmp_path / "alt_regime_shadow_state.json").read_text())
    assert len(st["positions"]) <= 3
    lines = [json.loads(x) for x in (tmp_path / "alt_regime_shadow.jsonl").read_text().splitlines()]
    assert lines and all(x["shadow"] for x in lines)
    assert sorted(p for p in tmp_path.iterdir()) == sorted(
        [tmp_path / "alt_regime_shadow.jsonl", tmp_path / "alt_regime_shadow_state.json"])
    assert not s.due(now)  # bar already processed
    assert r["errors"] == []


def test_refuses_without_paper_locks():
    class S:  # noqa: D401
        paper_trading, dry_run, allow_live_trading, alt_regime_shadow_enabled = True, True, True, True
    assert build_alt_shadow(S()) is None
