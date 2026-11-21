"""Edge validation for the put credit spread rules via tastytrade's backtester.

tastytrade runs option backtests server-side on its own historical chains
(``https://backtester.vast.tastyworks.com``).  That is the honest test for this
sleeve: a model-priced backtest would assume the very volatility premium we are
trying to verify.

What we can and cannot express there
------------------------------------
* Expressible: underlying, DTE at entry, short/long strikes by delta, take
  profit %, stop loss %, exit at N DTE, a minimum-VIX entry filter, max open
  trades.
* Not expressible: tastytrade IV rank (we use ``minimumVIX`` as the proxy) and
  the bid/ask quality gates (applied live only).  Long strike is chosen by delta
  rather than fixed width, so widths in the backtest are wider than the live
  default.  Treat results as a test of the *premise and management rules*.

Fees: per-spread fees from settings are subtracted from every trial.  If the
backtester already nets fees this double counts — deliberately conservative.

Promotion gate (all must hold on the out-of-sample half, after fees):
  trades >= 40, expectancy > 0, profit factor >= 1.2, and the in-sample half
  also profitable (consistency, not one lucky regime).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .config import Settings
from .credit_spread import close_fees, open_fees
from .tastytrade_client import BACKTESTER_HOST, TastytradeClient


@dataclass
class Variant:
    name: str
    short_delta: int
    long_delta: int
    dte: int
    take_profit_pct: int
    stop_loss_pct: int
    exit_dte: int
    min_vix: int | None = None

    def body(self, symbol: str, start: str, end: str, max_open: int = 1) -> dict:
        entry = {"frequency": "every day", "maximumActiveTrials": max_open,
                 "maximumActiveTrialsBehavior": "don't enter"}
        if self.min_vix:
            entry["minimumVIX"] = self.min_vix
        return {
            "symbol": symbol, "startDate": start, "endDate": end,
            "legs": [
                {"type": "equity-option", "direction": "short", "side": "put", "quantity": 1,
                 "strikeSelection": "delta", "delta": self.short_delta,
                 "daysUntilExpiration": self.dte},
                {"type": "equity-option", "direction": "long", "side": "put", "quantity": 1,
                 "strikeSelection": "delta", "delta": self.long_delta,
                 "daysUntilExpiration": self.dte},
            ],
            "entryConditions": entry,
            "exitConditions": {"takeProfitPercentage": self.take_profit_pct,
                               "stopLossPercentage": self.stop_loss_pct,
                               "atDaysToExpiration": self.exit_dte},
        }


def default_variants(s: Settings) -> list[Variant]:
    tp = int(round(s.options_take_profit_pct * 100))
    sl = int(round(s.options_stop_loss_multiple * 100))
    d = int(round(s.options_short_delta * 100))
    out = []
    for sd, ld in ((d, max(5, d - 10)), (16, 8), (30, 15)):
        for vix in (None, 18):
            out.append(Variant(name=f"{sd}d/{ld}d tp{tp} sl{sl} {s.options_dte_target}dte"
                                    f"{' vix>=' + str(vix) if vix else ''}",
                               short_delta=sd, long_delta=ld, dte=s.options_dte_target,
                               take_profit_pct=tp, stop_loss_pct=sl, exit_dte=s.options_exit_dte,
                               min_vix=vix))
    # De-duplicate when the configured delta equals one of the fixed probes.
    seen, uniq = set(), []
    for v in out:
        if v.name not in seen:
            seen.add(v.name)
            uniq.append(v)
    return uniq


def score_trials(trials: list[dict], fee_per_trade: float) -> dict:
    pnls = [float(t.get("profitLoss") or 0.0) - fee_per_trade for t in trials]
    n = len(pnls)
    if n == 0:
        return {"trades": 0}
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 3),
        "expectancy": round(sum(pnls) / n, 2),
        "total": round(sum(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "worst": round(min(pnls), 2),
        "profit_factor": round(sum(wins) / sum(losses), 2) if losses and sum(losses) > 0 else None,
        "max_drawdown": round(dd, 2),
    }


def split_verdict(trials: list[dict], fee_per_trade: float) -> dict:
    ordered = sorted(trials, key=lambda t: str(t.get("openDateTime")))
    half = len(ordered) // 2
    ins, oos = score_trials(ordered[:half], fee_per_trade), score_trials(ordered[half:], fee_per_trade)
    pf = oos.get("profit_factor")
    checks = {
        "oos_trades>=40": oos.get("trades", 0) >= 40,
        "oos_expectancy>0": oos.get("expectancy", -1) > 0,
        # pf None with trades means no losing trade at all, which passes.
        "oos_profit_factor>=1.2": (pf >= 1.2) if pf is not None else oos.get("trades", 0) > 0,
        "in_sample_profitable": ins.get("expectancy", -1) > 0,
    }
    return {"in_sample": ins, "out_of_sample": oos, "full": score_trials(ordered, fee_per_trade),
            "checks": checks, "promote": all(checks.values())}


class TastyBacktester:
    def __init__(self, client: TastytradeClient, *, poll_seconds: float = 5.0,
                 timeout_seconds: float = 900.0, sleep_fn=time.sleep):
        self.client = client
        self.poll = poll_seconds
        self.timeout = timeout_seconds
        self.sleep = sleep_fn

    def available(self) -> dict:
        return self.client.get("/available-dates", base=BACKTESTER_HOST)

    def run(self, body: dict) -> dict:
        res = self.client.post("/backtests", body, base=BACKTESTER_HOST)
        bt = res.get("data", res)
        waited = 0.0
        while bt.get("status") != "completed":
            if waited >= self.timeout:
                raise TimeoutError(f"backtest {bt.get('id')} still {bt.get('status')} after {waited:.0f}s")
            self.sleep(self.poll)
            waited += self.poll
            got = self.client.get(f"/backtests/{bt['id']}", base=BACKTESTER_HOST)
            bt = got.get("data", got)
        return bt


def run_validation(settings: Settings, *, symbols: list[str] | None = None,
                   start: str = "2018-01-01", end: str | None = None,
                   client: TastytradeClient | None = None,
                   backtester: TastyBacktester | None = None,
                   out_dir: Path = Path("reports")) -> dict:
    client = client or TastytradeClient(
        refresh_token=settings.tastytrade_refresh_token,
        client_secret=settings.tastytrade_client_secret,
        env="production",  # backtester data lives behind production auth
        timeout=settings.http_timeout_seconds)
    bt = backtester or TastyBacktester(client)
    end = end or date.today().isoformat()
    fee = open_fees(1, settings) + close_fees(1, settings) + 2 * settings.options_slippage_per_spread * 100
    rows = []
    for sym in symbols or settings.options_underlyings[:1]:
        for v in default_variants(settings):
            try:
                result = bt.run(v.body(sym, start, end))
                verdict = split_verdict(result.get("trials") or [], fee)
                rows.append({"symbol": sym, "variant": asdict(v), "name": v.name,
                             "notices": result.get("notices") or [], **verdict})
            except Exception as exc:  # one bad variant must not sink the report
                rows.append({"symbol": sym, "name": v.name, "error": str(exc)})
    report = {"generated": date.today().isoformat(), "range": [start, end],
              "fee_and_slippage_per_spread": round(fee, 2), "results": rows,
              "any_promotable": any(r.get("promote") for r in rows)}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"credit_spread_backtest_{report['generated']}.json").write_text(
        json.dumps(report, indent=2))
    return report


def format_report(report: dict) -> str:
    lines = [f"Put credit spread validation {report['range'][0]} -> {report['range'][1]}",
             f"costs per spread (fees + 2x slippage): ${report['fee_and_slippage_per_spread']:.2f}",
             f"{'symbol':6} {'variant':34} {'n':>4} {'win%':>5} {'exp$':>7} {'PF':>5} "
             f"{'maxDD$':>8}  promote"]
    for r in report["results"]:
        if "error" in r:
            lines.append(f"{r['symbol']:6} {r['name']:34} ERROR {r['error']}")
            continue
        o = r["out_of_sample"]
        lines.append(f"{r['symbol']:6} {r['name']:34} {o.get('trades', 0):>4} "
                     f"{100 * (o.get('win_rate') or 0):>5.0f} {o.get('expectancy', 0):>7.2f} "
                     f"{(o.get('profit_factor') or 0):>5.2f} {o.get('max_drawdown', 0):>8.0f}  "
                     f"{'YES' if r['promote'] else 'no'}")
    lines.append("(columns are the out-of-sample half; see JSON for in-sample and full)")
    return "\n".join(lines)
