#!/usr/bin/env python3
"""Markdown report writer for the daily trend quick check."""
from __future__ import annotations
import math
from pathlib import Path
from lib import ASSETS, START_EQUITY, fmt_pct, fmt_num, fmt_pf

OUT_REPORT = Path("/workspace/mayo-bot/reports/daily_trend_quickcheck_2026-11-15.md")

def mline(name, m):
    if not m or m.get("empty"):
        return f"| {name} | — | — | — | — | — | — | — | — |"
    return (f"| {name} | {m.get('tr_n', '—')} | {fmt_pct(m.get('tr_win'), 0)} | "
            f"{fmt_pf(m.get('tr_pf'))} | {fmt_pct(m.get('cagr'), 1)} | {fmt_pct(m.get('maxdd'), 1)} | "
            f"{fmt_num(m.get('sharpe'), 2)} | {fmt_pct(m.get('tot_ret'), 1)} | "
            f"${fmt_num(m.get('final_eq'), 2)} |")

def fold_str(folds):
    if not folds:
        return "—"
    bits = []
    for f in folds:
        s = fmt_pct(f["tot_ret"], 1)
        if f["tot_ret"] > 0:
            s = "+" + s
        bits.append(f"{f['year']} {s}")
    pos = sum(1 for f in folds if f["tot_ret"] > 0)
    return f"{pos}/{len(folds)} positive (" + "; ".join(bits) + ")"

def write_report(P, VARIANTS):
    primary, best = P["primary"], P["best_test100_80bps"]
    last_px, minn = P["last_px"], P["min_notional_now"]
    bh80_test = P["buy_and_hold"]["80bps"]["test"]
    bh0_test = P["buy_and_hold"]["0bps"]["test"]
    bh0_ytd = P["buy_and_hold"]["0bps"]["ytd2026"]
    p80, b80 = P["variants"][primary]["80bps"], P["variants"][best]["80bps"]
    bar, m_test = p80["bar_test100"], p80["test100"]
    m_full, m_dev, m_ytd = p80["full"], p80["dev"], p80["ytd2026"]
    unf = p80["unfillable_test100"]
    reasons = []
    if not bar["n_ok"]:
        reasons.append(f"only {m_test.get('tr_n', 0)} trades in the 2024–Nov 2026 $100 test (need ≥30)")
    if not bar["pf_ok"]:
        reasons.append(f"profit factor {fmt_pf(m_test.get('tr_pf'))} after 80 bps (need ≥1.10)")
    if not bar["majority"]:
        reasons.append(f"yearly folds in the test window were {bar['pos_folds']}/{bar['n_folds']} positive (need a majority)")
    if not bar["cash_ok"]:
        reasons.append(f"total return {fmt_pct(m_test.get('tot_ret'))} does not beat cash")
    if (unf.get("unfillable_frac") or 0) >= 0.10:
        reasons.append(
            f"{fmt_pct(unf['unfillable_frac'], 0)} of intended $100-account fills are below Kraken minimums "
            f"(median notional ${fmt_num(unf.get('median_notional'), 2)})"
        )
    lots_ok = (unf.get("unfillable_frac") or 0) < 0.25
    beats_bh = (not m_test.get("empty")) and (not bh0_test.get("empty")) and m_test["tot_ret"] > bh0_test["tot_ret"]
    if not beats_bh:
        reasons.append(
            f"test return {fmt_pct(m_test.get('tot_ret'))} does not beat buy-and-hold "
            f"{fmt_pct(bh0_test.get('tot_ret'))} (zero-cost) / {fmt_pct(bh80_test.get('tot_ret'))} (80 bps entry)"
        )
    go = "GO" if (bar["ok"] and lots_ok and beats_bh) else "NO-GO"

    L = []
    A = L.append
    A("# Daily trend-following quick check — BTC, ETH, SOL")
    A("")
    A("November 15, 2026 · research only · paper-trading implication: **$100 Kraken spot, no live orders**")
    A("")
    A(f"**Verdict: {go} for a $100 Kraken spot test of daily time-series trend following.**")
    A("")
    if go == "NO-GO":
        A(
            "The primary rule is a close copy of Zarattini, Pagani and Barbon (2025), "
            "applied to BTC, ETH and SOL only, with sleeve leverage capped at 100% so it can run on spot. "
            "At Kraken tier-1 maker fees (80 bps round trip) on a fresh $100 book from 1 Jan 2024 to 14 Nov 2026, "
            f"it {'does' if bar['ok'] else 'does not'} clear the promotion bar"
            + (f". Reasons: {'; '.join(reasons)}." if reasons else ".")
        )
    else:
        A(
            "The primary rule — Zarattini Combo with 25% volatility targeting and a 100% sleeve cap — "
            "clears the promotion bar at 80 bps on the 2024–Nov 2026 $100 test. "
            "Minimum sizes are generally satisfiable for entries and exits, though some volatility rebalances are tight."
        )
    A("")
    A("## 1. Direct answer")
    A("")
    A("**Primary rule (pre-registered, not picked after the fact):** equal-weight ensemble of nine long-only Donchian models on *closes* with lookbacks 5, 10, 20, 30, 60, 90, 150, 250 and 360 days. Enter when the close makes an n-day closing high. Exit when the close falls through a trailing stop that starts at the Donchian midpoint and only ratchets up. Each sleeve is sized to 25% annualized volatility using 90-day realized volatility, capped at 100% of that coin’s one-third sleeve (so the book never needs margin). Volatility-driven rebalances fire only if the sleeve weight drifts by more than 20 percentage points; entries and exits always fill. Capital is split equally across whichever of BTC, ETH and SOL have finished warmup. Signals are taken at the daily close and filled at the next day’s open.")
    A("")
    A("That is the Zarattini et al. 2025 Combo rule, with two intentional changes for a $100 Kraken *spot* account: the paper’s 200% sleeve cap is cut to 100%, and the paper’s 20-coin monthly rotational universe is replaced by these three names. The 200% version is reported as a reference variant; it is not tradable on cash spot.")
    A("")
    A(
        f"**$100 test, 80 bps round trip, 1 Jan 2024 – 14 Nov 2026 (primary):** {m_test['tr_n']} trades, "
        f"win rate {fmt_pct(m_test['tr_win'], 0)}, avg win ${fmt_num(m_test['tr_avg_win'], 2)} / avg loss ${fmt_num(m_test['tr_avg_loss'], 2)}, "
        f"payoff {fmt_num(m_test['tr_payoff'], 2)}, profit factor {fmt_pf(m_test['tr_pf'])}, CAGR {fmt_pct(m_test['cagr'])}, "
        f"max drawdown {fmt_pct(m_test['maxdd'])}, Sharpe {fmt_num(m_test['sharpe'])}, total return {fmt_pct(m_test['tot_ret'])} "
        f"(${fmt_num(START_EQUITY, 0)} → ${fmt_num(m_test['final_eq'], 2)}). Yearly folds: {fold_str(p80['folds_test'])}."
    )
    A("")
    A(
        f"**Buy-and-hold, same window:** zero-cost equal-weight {fmt_pct(bh0_test.get('tot_ret'))} "
        f"(CAGR {fmt_pct(bh0_test.get('cagr'))}, max DD {fmt_pct(bh0_test.get('maxdd'))}, Sharpe {fmt_num(bh0_test.get('sharpe'))}); "
        f"with an 80 bps entry cost {fmt_pct(bh80_test.get('tot_ret'))}. Cash is 0%. "
        f"Primary {'beats' if m_test['tot_ret']>0 else 'does not beat'} cash and "
        f"{'beats' if beats_bh else 'does not beat'} buy-and-hold."
    )
    A("")
    A(
        f"**2026 YTD (1 Jan – 14 Nov 2026), 80 bps, primary on the continuing full-sample book:** "
        f"return {fmt_pct(m_ytd.get('tot_ret') if not m_ytd.get('empty') else float('nan'))}, "
        f"Sharpe {fmt_num(m_ytd.get('sharpe') if not m_ytd.get('empty') else float('nan'))}, "
        f"max DD {fmt_pct(m_ytd.get('maxdd') if not m_ytd.get('empty') else float('nan'))}, "
        f"trades {m_ytd.get('tr_n')}, PF {fmt_pf(m_ytd.get('tr_pf') if not m_ytd.get('empty') else float('nan'))}. "
        f"Buy-and-hold zero-cost YTD {fmt_pct(bh0_ytd.get('tot_ret'))}."
    )
    A("")
    A(
        f"**Bar (test $100, 80 bps, primary):** trades ≥30: {'yes' if bar['n_ok'] else 'no'}; "
        f"PF ≥1.10: {'yes' if bar['pf_ok'] else 'no'}; majority of yearly folds positive: "
        f"{'yes' if bar['majority'] else 'no'} ({bar['pos_folds']}/{bar['n_folds']}); "
        f"beats cash: {'yes' if bar['cash_ok'] else 'no'}. **Overall bar: {'PASS' if bar['ok'] else 'FAIL'}.**"
    )
    A("")
    if best != primary:
        bm = b80["test100"]
        A(
            f"**Empirically best of the 12 on this same $100 80 bps test (selection-biased, not the pre-registered call):** "
            f"`{best}` — {bm['tr_n']} trades, PF {fmt_pf(bm['tr_pf'])}, CAGR {fmt_pct(bm['cagr'])}, "
            f"Sharpe {fmt_num(bm['sharpe'])}, max DD {fmt_pct(bm['maxdd'])}, total return {fmt_pct(bm['tot_ret'])}. "
            "This is reported for completeness; it is not a reason to go live."
        )
        A("")
    A(
        f"**Go/no-go:** **{go}.** "
        + (" ".join(reasons) if reasons else
           "The pre-registered rule clears the bar at 80 bps on the $100 2024–2026 test and is tradable at Kraken minimums for entries and exits.")
    )
    A("")
    A("## 2. Data, costs, and minimum size")
    A("")
    A("- **Prices:** Binance Vision public spot daily klines (USDT pairs). `api.binance.com` returned HTTP 451 from this host, so the S3 Vision archive was used. BTC and ETH run 17 Aug 2017 – 14 Nov 2026; SOL runs 11 Aug 2020 – 14 Nov 2026. The 15 Nov 2026 bar was still open and was dropped. Signals use UTC daily closes; fills use the next UTC open.")
    basis_txt = ", ".join(
        f"{a} median basis {fmt_num(P['basis'][a].get('median_bps'), 1)} bps, mean absolute {fmt_num(P['basis'][a].get('mean_abs_bps'), 1)} bps"
        for a in ASSETS if P['basis'][a].get('n')
    )
    A(f"- **Kraken check:** public OHLC `interval=1440` only returns 720–721 bars (~25 Nov 2024 – 15 Nov 2026). On the overlap, Kraken USD vs Binance USDT closes were tight: {basis_txt}. The primary rule was re-run on Kraken prices in that overlap as a robustness check (section 6).")
    A("- **Costs:** 10 bps round trip (paper’s published net figure, and a Binance-like reference), 80 bps (Kraken tier-1 maker 40 bps each way, no slippage — best case), 160 bps (stress; about taker 80 bps each way). There is no mid-tier. All reported P&L below is net of the stated cost.")
    A(
        f"- **Kraken minimums** (public `AssetPairs`, 15 Nov 2026): BTC `ordermin` 0.00006, ETH 0.002, SOL 0.05; `costmin` $0.50 on all three. "
        f"At the 14 Nov 2026 Binance close (BTC ${fmt_num(last_px['BTC'], 0)}, ETH ${fmt_num(last_px['ETH'], 2)}, SOL ${fmt_num(last_px['SOL'], 2)}) "
        f"the binding minimum notionals are about **${fmt_num(minn['BTC'], 2)} / ${fmt_num(minn['ETH'], 2)} / ${fmt_num(minn['SOL'], 2)}**. "
        "A $100 book split three ways is ~$33 per coin, which clears those minimums for a full sleeve entry. Volatility downsizing and small rebalances may not."
    )
    A(
        f"- **$100 test fills, primary, 80 bps:** {unf['n_fills']} intended fills, {unf['n_unfillable']} ({fmt_pct(unf['unfillable_frac'], 0)}) below the Kraken minimum, "
        f"accounting for {fmt_pct(unf['unfillable_turnover_frac'], 0)} of turnover. Median fill notional ${fmt_num(unf['median_notional'], 2)} "
        f"(range ${fmt_num(unf['min_notional_seen'], 2)}–${fmt_num(unf['max_notional_seen'], 2)}). "
        "The backtest *does not* skip those undersize fills — it assumes they go through — so live P&L would be slightly worse if they are rejected. "
        "Entries and exits are usually large enough; the undersize tickets are mostly volatility rebalances."
    )
    A("- **Warmup:** Combo needs 360 daily bars (and 90 days of returns for vol). BTC/ETH can trade from August 2018; SOL joins around August 2021. Until SOL is ready, capital is split equally across the coins that are ready. Buy-and-hold does the same, then adds SOL with a one-time rebalance.")
    A("")
    A("## 3. Paper rule, and what we changed")
    A("")
    A('Source: Carlo Zarattini, Alberto Pagani, Andrea Barbon, “Catching Crypto Trends; A Tactical Approach for Bitcoin and Altcoins,” Swiss Finance Institute Research Paper No. 25-80, this version 9 April 2025, [SSRN 5209907](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5209907), PDF from [Concretum](https://concretumgroup.com/wp-content/uploads/2026/02/Catching-Crypto-Trends.pdf).')
    A("")
    A("Copied exactly:")
    A("- Donchian channels computed on *closes*, not highs/lows: upper = max of the last n closes, lower = min, mid = average of the two.")
    A("- Enter when close equals the upper band; stay long otherwise; exit when close ≤ trailing stop.")
    A("- Trailing stop used on day t+1 is max(stop used on day t, mid on day t). It never ratchets down. New trades start with stop = mid at entry.")
    A("- Nine lookbacks: 5, 10, 20, 30, 60, 90, 150, 250, 360. Combo weight is the equal-weight average of the nine sleeve weights.")
    A("- Sleeve weight = min(0.25 / σ_t, cap) × position, with σ_t = 90-day (3-month) annualized standard deviation of daily returns, annualized with 365 days.")
    A("- 20% rebalance threshold applies only to volatility-driven weight changes; a new entry or stop fills immediately.")
    A("")
    A("Changed for this test:")
    A("- Universe is BTC, ETH, SOL, not a monthly top-20 volume rotation. Their headline Sharpe 1.57 and 11% max DD is for that 20-coin book, net of 10 bps, 2015–19 Mar 2025. It is not a claim about a 3-coin $100 Kraken account.")
    A("- Sleeve cap 100% in the primary (spot). The paper allows 200%. That variant is included; it rarely differs because crypto volatility usually keeps 0.25/σ well under 1.")
    A("- Execution is next-open, not close-to-close. Fees are charged on every fill’s absolute weight change.")
    A("- Sample runs through 14 Nov 2026, so it includes the post-paper year the authors could not have seen.")
    A("")
    br = P["btc_paper_replication_10bps"]
    A(
        f"**BTC-only close-to-close replication at 10 bps, 200% cap, 20% threshold** "
        f"(the closest apples-to-apples check we can run on this data): from {br['start']} to {br['end']}, "
        f"CAGR {fmt_pct(br['cagr'])}, vol {fmt_pct(br['vol'])}, Sharpe {fmt_num(br['sharpe'])}, "
        f"max DD {fmt_pct(br['maxdd'])}, total return {fmt_pct(br['tot_ret'])}. "
        "The paper’s BTC Combo, Jan 2015–19 Mar 2025, was 30% CAGR, 17% vol, Sharpe 1.58 *gross* / 1.56 net of 10 bps, max DD 19%. "
        "We start later (miss 2015–17) and include 2025–26, so a lower number is not a coding failure by itself; it is a different window. "
        "If this Sharpe had come out near zero or negative, that would have been a warning that the rule was mis-implemented."
    )
    A("")
    A("## 4. All 12 variants at 80 bps")
    A("")
    A("Twelve variants were specified in advance. No other lookbacks or filters were tried. The primary is row 1. Trade counts, PF and win rate are at the *coin-level round trip* (a trade starts when that coin’s weight leaves 0 and ends when it returns to 0; Combo can stay lightly long in a coin while some sleeves are off). CAGR, Sharpe, max DD and total return are on the $100 daily equity path.")
    A("")
    A("### 4.1 $100 book, 1 Jan 2024 – 14 Nov 2026 (the decision window)")
    A("")
    A("| Variant | Trades | Win | PF | CAGR | Max DD | Sharpe | Total return | Final $ |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for v in VARIANTS:
        name = v["name"]
        if v["id"] == primary:
            name = f"**{name} (primary)**"
        A(mline(name, P["variants"][v["id"]]["80bps"]["test100"]))
    A(mline("Buy-and-hold (80 bps entry)", bh80_test))
    A(mline("Buy-and-hold (zero cost)", bh0_test))
    A(mline("Cash", dict(empty=False, tr_n=0, tr_win=float("nan"), tr_pf=float("nan"),
                           cagr=0.0, maxdd=0.0, sharpe=float("nan"), tot_ret=0.0, final_eq=START_EQUITY)))
    A("")
    A("Same window at **10 bps** and **160 bps** for the primary and the empirically best variant:")
    A("")
    A("| Variant / cost | Trades | Win | PF | CAGR | Max DD | Sharpe | Total return | Final $ |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    shown = [(primary, "Primary")]
    if best != primary:
        shown.append((best, "Best-on-test"))
    for vid, label in shown:
        for cname in ["10bps", "80bps", "160bps"]:
            A(mline(f"{label} `{vid}` @ {cname}", P["variants"][vid][cname]["test100"]))
    A("")
    A("### 4.2 Pre-2024 development window (through 31 Dec 2023), 80 bps")
    A("")
    A("This is a split, not a fitted walk-forward: the rules were not tuned on this window. Equity here is sliced from the continuing full-sample book (not a fresh $100).")
    A("")
    A("| Variant | Trades | Win | PF | CAGR | Max DD | Sharpe | Total return | $100 grew to |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for v in VARIANTS:
        name = v["name"] + (" (primary)" if v["id"] == primary else "")
        A(mline(name, P["variants"][v["id"]]["80bps"]["dev"]))
    A(mline("Buy-and-hold (80 bps entry)", P["buy_and_hold"]["80bps"]["dev"]))
    A(mline("Buy-and-hold (zero cost)", P["buy_and_hold"]["0bps"]["dev"]))
    A("")
    A("### 4.3 Full sample, 80 bps (continuing book)")
    A("")
    A("| Variant | Trades | Win | PF | CAGR | Max DD | Sharpe | Total return | $100 grew to | Yearly folds |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for v in VARIANTS:
        name = v["name"] + (" (primary)" if v["id"] == primary else "")
        m = P["variants"][v["id"]]["80bps"]["full"]
        fs = fold_str(P["variants"][v["id"]]["80bps"]["folds_full"])
        if not m or m.get("empty"):
            A(f"| {name} | — | — | — | — | — | — | — | — | — |")
        else:
            A(
                f"| {name} | {m['tr_n']} | {fmt_pct(m['tr_win'], 0)} | {fmt_pf(m['tr_pf'])} | "
                f"{fmt_pct(m['cagr'], 1)} | {fmt_pct(m['maxdd'], 1)} | {fmt_num(m['sharpe'], 2)} | "
                f"{fmt_pct(m['tot_ret'], 1)} | ${fmt_num(m['final_eq'], 2)} | {fs} |"
            )
    A("")
    A("### 4.4 2026 year-to-date, 80 bps")
    A("")
    A("| Variant | Trades | Win | PF | Total return | Sharpe | Max DD |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    for v in VARIANTS:
        name = v["name"] + (" (primary)" if v["id"] == primary else "")
        m = P["variants"][v["id"]]["80bps"]["ytd2026"]
        if not m or m.get("empty"):
            A(f"| {name} | — | — | — | — | — | — |")
        else:
            A(f"| {name} | {m['tr_n']} | {fmt_pct(m['tr_win'], 0)} | {fmt_pf(m['tr_pf'])} | {fmt_pct(m['tot_ret'], 1)} | {fmt_num(m['sharpe'], 2)} | {fmt_pct(m['maxdd'], 1)} |")
    A(f"| Buy-and-hold (zero cost) | — | — | — | {fmt_pct(bh0_ytd.get('tot_ret'), 1)} | {fmt_num(bh0_ytd.get('sharpe'), 2)} | {fmt_pct(bh0_ytd.get('maxdd'), 1)} |")
    A("")
    A("## 5. Promotion bar, all 12, $100 test at 80 bps")
    A("")
    A("| Variant | ≥30 trades | PF ≥1.10 | Majority folds + | Beats cash | Bar |")
    A("|---|:---:|:---:|:---:|:---:|:---:|")
    n_pass = 0
    for v in VARIANTS:
        b = P["variants"][v["id"]]["80bps"]["bar_test100"]
        n_pass += int(b["ok"])
        A(
            f"| {v['name']}{' (primary)' if v['id']==primary else ''} | "
            f"{'yes' if b['n_ok'] else 'no'} | {'yes' if b['pf_ok'] else 'no'} | "
            f"{'yes' if b['majority'] else 'no'} ({b['pos_folds']}/{b['n_folds']}) | "
            f"{'yes' if b['cash_ok'] else 'no'} | **{'PASS' if b['ok'] else 'FAIL'}** |"
        )
    A("")
    A(f"{n_pass} of 12 variants pass the bar at 80 bps on the $100 2024–Nov 2026 test. Variants counted honestly: **12**. No others were run and discarded.")
    A("")
    A("## 6. Permutation test and Kraken robustness")
    A("")
    for vid, po in (P.get("permutation") or {}).items():
        label = "primary" if vid == primary else "empirically best on test"
        A(
            f"**{label} `{vid}`**, 1,000 shuffles of each coin’s daily return packages (intraday high/low/open shape kept, serial order destroyed), "
            f"same 80 bps $100 test window, p-values with a +1 Laplace correction: "
            f"p(Sharpe ≥ actual {fmt_num(po['actual_sharpe'])}) = {fmt_num(po.get('p_sharpe'), 3)}; "
            f"p(CAGR ≥ actual {fmt_pct(po.get('actual_cagr'))}) = {fmt_num(po.get('p_cagr'), 3)}; "
            f"p(PF ≥ actual {fmt_pf(po.get('actual_pf'))}) = {fmt_num(po.get('p_pf'), 3)}. "
            f"Valid runs: {po.get('valid')}/{po.get('n')}. Permutation Sharpe median {fmt_num(po.get('perm_sharpe_median'))}, "
            f"95th percentile {fmt_num(po.get('perm_sharpe_p95'))}."
        )
        A("")
    kr = P.get("kraken_robustness_primary_80bps") or {}
    if kr and not kr.get("empty"):
        A(
            f"**Kraken-price robustness, primary, 80 bps, overlap {kr['start']} – {kr['end']}** "
            f"(Binance history used only for warmup, then Kraken USD OHLC): "
            f"{kr['tr_n']} trades, PF {fmt_pf(kr['tr_pf'])}, total return {fmt_pct(kr['tot_ret'])}, "
            f"CAGR {fmt_pct(kr['cagr'])}, Sharpe {fmt_num(kr['sharpe'])}, max DD {fmt_pct(kr['maxdd'])}. "
            "This is a shorter window than the full test; it is a price-source check, not a second test period."
        )
        A("")
    A("## 7. What this means for a $100 Kraken account")
    A("")
    A(f"**{go}.** " + ("Do not take this live, even with $100. " if go == "NO-GO" else "A paper test is still the right next step — not live. "))
    if go == "NO-GO":
        A(
            "Daily trend following has a real published pedigree, and the BTC-only 10 bps replication is in a plausible range versus the paper, "
            "but the version that actually fits this account — three coins, next-open fills, 80 bps, no leverage, $100 — "
            "does not earn the promotion bar and/or does not beat sitting in the coins. "
            "Kraken minimums are also tight enough that a vol-targeted $100 book will have rejected or skipped rebalances."
        )
    else:
        A(
            "The pre-registered Combo rule cleared the bar on the 2024–2026 $100 test at maker fees. "
            "That is encouraging and consistent with the published evidence, but it is still one three-year window, "
            "with fewer than a long-run sample of independent trades, and it has not been promoted by this program’s own walk-forward-plus-holdout standard. "
            "If anything is done next, it should be a paper-only shadow of this exact rule, not a live $100 ticket."
        )
    A("")
    A("Practical constraints, regardless of the verdict:")
    A(f"- Minimum notionals of ~${fmt_num(minn['BTC'], 0)}–${fmt_num(minn['SOL'], 0)} mean you cannot finely vol-target a $100 book. Equal 1/N entries (the `*_eq` variants) are the only sizes that comfortably clear the minimums.")
    A("- 80 bps round trip is the *best* Kraken case (maker-maker, no slip). A market ticket at the daily open costs about 160 bps round trip and is the more realistic number. Look at the 160 bps column before treating 80 bps as earned.")
    A("- Spot cannot use the paper’s 200% cap. That is a smaller issue than fees, because 25% vol-targeting in crypto usually wants well under 100%.")
    A("- This study does not authorize any flag change, restart, or live ticket.")
    A("")
    A("## 8. Method notes")
    A("")
    A("- Variant count: 12. Primary specified first. The “best on test” label, if different, is exploratory.")
    A("- Folds are calendar years. The test window has 2024, 2025 and 2026 (through 14 Nov). That is a short fold set; a majority means at least 2 of 3 years positive.")
    A("- Trade-level PF uses dollar P&L on the $100 book, including fees attributed to that coin. Open trades at the end of a window are marked to the last open and counted.")
    A("- Sharpe and CAGR annualize with 365 days. Drawdown is peak-to-trough of the daily equity curve.")
    A("- Buy-and-hold is a static equal-weight book of the coins that are listed at the start of the window, with SOL added and weights reset to 1/3 when it appears. It is not daily-rebalanced (that would be a fee-heavy straw man at 80 bps).")
    A("- Permutation shuffles each coin independently. It asks whether the rule’s test-window Sharpe/PF could be luck from the order of daily moves. It is not a full data-mined DSR across other strategies in this program’s earlier research.")
    A("- No trading code, flags, `box_env.sh`, logs, or data permissions were changed. No keys were used. No tickets were sent. Raw price zips were cached under `/workspace/edge2-data/daily_trend/` and are not committed.")
    A("")
    A("## 9. Sources")
    A("")
    A("- Zarattini, C., Pagani, A., and Barbon, A. (2025), “Catching Crypto Trends; A Tactical Approach for Bitcoin and Altcoins,” Swiss Finance Institute Research Paper No. 25-80, SSRN 5209907.")
    A("- Binance Vision spot daily klines: `https://data.binance.vision/data/spot/monthly/klines/{SYMBOL}/1d/` and daily files for 1–14 Nov 2026.")
    A("- Kraken public REST: `/0/public/OHLC?interval=1440` and `/0/public/AssetPairs` (15 Nov 2026).")
    A("")
    OUT_REPORT.write_text("\n".join(L) + "\n")
