import json
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from dublin_bot import futures_shadow as fs
from dublin_bot.config import Settings

DAY = 86400
H4 = 14400


def _daily_rows(closes, t0=0):
    return [{"time": (t0 + i * DAY) * 1000, "open": c, "high": c, "low": c, "close": c}
            for i, c in enumerate(closes)]


class Fake:
    """URL-routing fake opener. Records calls."""

    def __init__(self):
        self.daily = {}
        self.h4 = {}
        self.marks = {"PF_XBTUSD": 100.0, "PF_ETHUSD": 100.0, "PF_SOLUSD": 100.0}
        self.rate_abs = 0.0
        self.funding = []
        self.calls = []
        self.fail = False
        self.delay = 0.0

    def __call__(self, url, timeout):
        self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OSError("network down")
        if "/tickers/" in url:
            perp = url.rsplit("/", 1)[1]
            return json.dumps({"ticker": {"symbol": perp, "markPrice": self.marks[perp],
                                          "fundingRate": self.rate_abs}}).encode()
        if "historical-funding-rates" in url:
            return json.dumps({"rates": self.funding}).encode()
        perp = url.split("/trade/")[1].split("/")[0]
        if "/1d?" in url:
            return json.dumps({"candles": self.daily.get(perp, [])}).encode()
        return json.dumps({"candles": self.h4.get(perp, [])}).encode()


def _feed(fake):
    return fs.PublicFeed(opener=fake, min_interval=0.0)


def setup_function(_):
    fs.PublicFeed._funding_cache.clear()


def test_d1flip_target_rules():
    t = np.arange(120) * DAY
    now = 121 * DAY
    up = pd.DataFrame({"time": t, "close": np.linspace(100, 220, 120)})
    down = pd.DataFrame({"time": t, "close": np.linspace(220, 100, 120)})
    assert fs.d1flip_target(up, now=now)[0] == 1
    assert fs.d1flip_target(down, now=now)[0] == -1
    assert fs.d1flip_target(up.iloc[:30], now=now)[0] == 0
    # rising then a dip below a still-rising SMA: strict = flat, repo = short
    mix = pd.DataFrame({"time": t, "close": np.r_[np.linspace(100, 200, 119), [150.0]]})
    assert fs.d1flip_target(mix, now=now, bear="strict")[0] == 0
    assert fs.d1flip_target(mix, now=now, bear="repo")[0] == -1


def _bars(closes):
    n = len(closes)
    c = np.asarray(closes, float)
    return pd.DataFrame({"time": np.arange(n) * H4, "open": c, "high": c, "low": c, "close": c})


def test_donchian_rules_and_partial_bar_ignored():
    base = [100.0] * 340
    closed = 341 * H4                  # bar 340 (time 340*H4) closes at 341*H4
    b = _bars(base + [130.0])
    assert fs.donchian_decide(b, 0, now=closed - 1)[0] == 0        # still open -> ignored
    assert fs.donchian_decide(b, 0, now=closed)[0] == 1            # closed breakout
    assert fs.donchian_decide(b, 1, now=closed)[0] == 1            # hold
    b = _bars(base + [90.0])
    assert fs.donchian_decide(b, 0, now=closed)[0] == -1
    assert fs.donchian_decide(b, 1, now=closed)[0] == -1           # flip
    # exit long below the 20-day low without a 55-day breakdown
    seq = [80.0] * 200 + [100.0] * 140 + [95.0]
    assert fs.donchian_decide(_bars(seq), 1, now=len(seq) * H4)[0] == 0
    assert fs.donchian_decide(_bars(base[:50]), 1, now=51 * H4)[1]["reason"].startswith("warming")


def _d1(tmp, fake, **kw):
    return fs.D1FlipShadow(logs_dir=tmp, feed=_feed(fake), symbols=["BTC/USD"], **kw)


def test_cycle_opens_once_per_bar_and_closes_with_costs(tmp_path):
    fake = Fake()
    now = 121 * DAY + 3600 * 2 + 600
    fake.daily["PF_XBTUSD"] = _daily_rows(list(np.linspace(100, 220, 121)))
    sh = _d1(tmp_path, fake)
    r = sh.run_cycle(now=now)
    assert r.ran and [a["event"] for a in r.actions if a["event"] != "decide"] == ["open"]
    n_calls = len(fake.calls)
    r2 = sh.run_cycle(now=now + 300)          # same 1h bar -> no network at all
    assert not r2.ran and len(fake.calls) == n_calls
    assert "PF_XBTUSD" in r2.positions
    # next bar: daily turns bearish, mark falls -> close long (+ open short)
    fake.daily["PF_XBTUSD"] = _daily_rows(list(np.linspace(220, 100, 121)))
    fake.marks["PF_XBTUSD"] = 90.0
    ts = int(now) + 3600
    fake.funding = [{"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                     "relativeFundingRate": 1e-4}]
    r3 = sh.run_cycle(now=now + 3600 + 120)
    ev = [a["event"] for a in r3.actions if a["event"] != "decide"]
    assert ev == ["close", "open"]
    tr = [json.loads(x) for x in sh.trades_path.read_text().splitlines()]
    assert len(tr) == 1 and tr[0]["side"] == "long" and tr[0]["shadow"] is True
    # gross -10%, fees 14 bps, one full funding hour paid by the long (-1 bps)
    assert abs(tr[0]["net_bps"] - (-1000 - 14 - 1)) < 0.01
    # only the shadow's own files were written
    assert sorted(p.name for p in Path(tmp_path).iterdir()) == [
        "futures_shadow_d1flip.jsonl", "futures_shadow_d1flip_state.json",
        "futures_shadow_d1flip_trades.jsonl"]


def test_funding_proration_and_short_receives(tmp_path):
    fake = Fake()
    sh = _d1(tmp_path, fake)
    pos = {"side": -1, "entry_ts": 1000.0, "funding_ts": 1000.0, "funding": 0.0}
    fake.funding = [{"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                     "relativeFundingRate": 2e-4} for t in (3600, 7200, 999999)]
    sh._accrue(pos, "PF_XBTUSD", now=8000)
    # first hour prorated (2600/3600), second hour full; future print ignored; short receives
    assert abs(pos["funding"] - 2e-4 * (2600 / 3600 + 1)) < 1e-12
    assert pos["funding_ts"] == 7200


def test_network_failure_is_contained_and_backs_off(tmp_path):
    fake = Fake()
    fake.fail = True
    sh = _d1(tmp_path, fake)
    now = 200 * DAY + 600
    r = sh.run_cycle(now=now)
    assert r.ran and r.errors and not r.positions
    st = sh.load()
    assert st["last_bar"] is None and st["last_error"]
    assert not sh.due(now + 60)                # backoff
    assert sh.due(now + fs.RETRY_AFTER_ERROR + 1)


def test_runner_time_boxed_swallows_and_no_overlap(tmp_path):
    logs = []
    fake = Fake()
    fake.delay = 0.5
    fake.daily["PF_XBTUSD"] = _daily_rows(list(np.linspace(100, 220, 121)))
    sh = _d1(tmp_path, fake)

    class Boom(fs.FuturesShadow):
        name = "boom"

        def due(self, now=None, state=None):
            raise RuntimeError("kaboom")

    runner = fs.ShadowRunner([sh, Boom(logs_dir=tmp_path, feed=_feed(fake))], logs.append, budget=0.1)
    t0 = time.monotonic()
    runner.tick()
    assert time.monotonic() - t0 < 0.4          # did not wait for the slow network
    runner.tick()                                # still running -> not started twice
    assert sum(1 for t in threading.enumerate() if t.name == "shadow-d1flip") == 1
    assert any("WARN SHADOW_FUTURES sleeve=boom" in x for x in logs)
    runner._threads["d1flip"].join(5)
    assert any(x.startswith("SHADOW_FUTURES sleeve=d1flip ran=True") for x in logs)
    assert not any(" ERROR " in x for x in logs)


def test_build_shadows_default_off_and_lock_gated(tmp_path):
    s = Settings(_env_file=None)
    assert fs.build_shadows(s) == []
    s.futures_shadow_d1flip_enabled = True
    s.futures_shadow_donchian_enabled = True
    names = [x.name for x in fs.build_shadows(s)]
    assert names == ["d1flip", "donchian4h"]
    s.allow_live_trading = True
    assert fs.build_shadows(s) == []


def test_promotion_bar(tmp_path):
    sh = fs.Donchian4hShadow(logs_dir=tmp_path, feed=_feed(Fake()))
    tmp_path.mkdir(exist_ok=True)
    with sh.trades_path.open("w") as fh:
        for i in range(31):
            fh.write(json.dumps({"net_bps": 50.0 + (i % 3), "pnl": 0.1}) + "\n")
    rep = sh.promotion()
    assert rep["trades"] == 31 and rep["passes"] is True


def test_no_order_route_or_book_imports():
    src = Path(fs.__file__).read_text()
    imports = "\n".join(ln for ln in src.splitlines() if ln.strip().startswith(("import ", "from ")))
    for bad in ("venue_locks", "kraken_gateway", "paper", "learner", "engine", "gateway"):
        assert bad not in imports
    for bad in ("submit_", "API_KEY", "API_SECRET", "paper_portfolio"):
        assert bad not in src
