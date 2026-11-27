# Day-trading tools (companion to arc_bot)

| File | What it is | Tested? |
|---|---|---|
| `orb_alert.pine` | TradingView ORB alert: OR high line, green triangle, light-green background, alert; includes the "top 50% of bar" refinement (toggle) | **not run** (needs TradingView) |
| `orb_alert.thinkscript` | ThinkorSwim version (label, bubble, alert; 1/5/15-min) | **not run** (needs ThinkorSwim) |
| `trailing_stop_2bar.pine` | 2-bar trailing-stop strategy (MA-cross demo entry), red->green stop line, entry/exit markers | **not run** |
| `trailing_stop_2bar.thinkscript` | ThinkorSwim version | **not run** |
| `analyze_trades.py` | Weekly journal analyzer: `python analyze_trades.py trades.csv` | yes (`tests/test_analyze_trades.py`) |

Paste the Pine/thinkScript files into TradingView's Pine editor / ThinkorSwim's thinkScript editor.
If either editor reports a syntax error, send me the exact message and I'll fix it.

Notes: ORB uses the exchange zone America/New_York (so "EST" is handled across DST). In TradingView, create the
alert with "Once Per Bar Close" so it fires only on confirmed bars. A thinkScript strategy's fills are
approximate; backtest results in ThinkorSwim and TradingView will differ slightly.
