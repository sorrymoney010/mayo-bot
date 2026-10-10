from __future__ import annotations

from pathlib import Path
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Canonical tradeable basket. Fixed and independent of the *currently selected*
# coin: BTC/USD is the master coin (always first/allowed) so it never disappears
# when the operator pins another coin. Order is stable; ``allowed_symbols``
# dedupes but preserves this canonical ordering.
DEFAULT_COIN_BASKET: tuple[str, ...] = (
    "BTC/USD",       # master coin
    "UNI/USD",       # Uniswap
    "XRP/USD",
    "PUMP/USD",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Active broker ──────────────────────────────────────
    broker: str = "kraken"  # kraken only (alpaca decommissioned)

    # ── Kraken Spot ─────────────────────────────────────────
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    kraken_tier: str = "starter"  # starter | intermediate | pro

    # ── Binance Spot ───────────────────────────────────────
    binance_api_key: str = ""
    binance_api_secret: str = ""

    # ── Coinbase Advanced Trade ────────────────────────────
    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""  # Coinbase uses a key *name* + PEM/passphrase

    # ── Bybit Spot (optional, future) ──────────────────────
    bybit_api_key: str = ""
    bybit_api_secret: str = ""

    # ── Multi-account: run the same engine across several brokers ──
    # Each entry is a broker name; the matching *_api_key/_secret are read
    # from the environment. Empty => single-account (use `broker`).
    accounts: list[str] = Field(default_factory=list)

    # ── Transport / reliability ────────────────────────────
    http_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    max_retries: int = Field(default=3, ge=1, le=6)

    # ── Data freshness ─────────────────────────────────────
    max_bar_age_multiple: float = Field(default=3.0, gt=0, le=20)
    max_clock_skew_seconds: float = Field(default=30.0, gt=0, le=300)

    # ── Market quality gates ───────────────────────────────
    max_spread_bps: float = Field(default=50.0, gt=0)
    min_dollar_volume: float = Field(default=1_000_000.0, ge=0)

    # ── Operational state paths ────────────────────────────
    audit_log_path: Path = Path("logs/audit.jsonl")
    idempotency_path: Path = Path("logs/orders.json")
    nonce_state_path: Path = Path("logs/nonce.json")
    session_state_path: Path = Path("logs/session_state.json")
    execution_db_path: Path = Path("logs/executions.sqlite3")

    paper_trading: bool = True
    allow_live_trading: bool = False
    dry_run: bool = True
    live_risk_acknowledgement: str = ""

    symbol: str = "BTC/USD"
    # Rapid mode: 15-minute bars, 15-minute monitor cadence, and a 15-minute
    # cooldown between entries. Strategy RSI/momentum gates are NOT loosened.
    timeframe_minutes: int = Field(default=15, ge=1)
    lookback_bars: int = Field(default=500, ge=220)
    # Day-trade mode: tighten to 5-minute bars and a 60-second monitor cadence
    # so the engine reacts intraday instead of every 15 minutes. Strategy gates
    # are NOT loosened — only the data resolution and polling speed change.
    # Applied at startup by TradingEngine._apply_performance_profile().
    day_trade_mode: bool = False
    strategy_equity_usd: float = Field(default=100.0, ge=25.0)
    # Auto-scale risk per trade based on session win/loss streak. The base risk
    # (risk_per_trade) is multiplied by this factor, which the engine moves
    # between min_risk_scale and max_risk_scale as the bot wins/loses — so a
    # winning streak compounds allocation up, a losing streak tightens it down.
    adaptive_risk: bool = Field(default=True)
    min_risk_scale: float = Field(default=0.5, gt=0, le=1.0)
    max_risk_scale: float = Field(default=2.0, gt=1.0)
    risk_step: float = Field(default=0.15, gt=0, le=0.5)
    # Canonical, always-allowed basket. Defaults to DEFAULT_COIN_BASKET and is
    # intentionally independent of the mutable ``symbol`` selection, so BTC/USD
    # (and the rest of the basket) is never lost when a different coin is pinned.
    coin_basket: list[str] = Field(default_factory=lambda: list(DEFAULT_COIN_BASKET))
    # ── Trading universe: how the bot discovers tradeable coins ──────
    # "basket"  -> only the coins in ``coin_basket`` (hard allowlist, default)
    # "all_usd" -> every active Kraken */USD spot pair above the min notional.
    #   The bot autonomously ranks the full market by strategy setup + learned
    #   expectancy and trades whatever coin the market is offering. This is the
    #   "full API access" mode — no manual coin section.
    universe_mode: str = Field(default="all_usd")
    # Hard safety allowlist applied ON TOP of the universe. Coins here are the
    # only ones the bot may ever touch. Default = the set with POSITIVE
    # backtested expectancy for the live mean-reversion strategy (audit #8:
    # the edge is coin-specific — XRP/SOL bleed, PUMP/BTC are positive). The
    # bot still ranks autonomously within this set; it just cannot leak on
    # negative-expectancy coins. Override via UNIVERSE_ALLOWLIST in .env.
    universe_allowlist: list[str] = Field(
        default_factory=lambda: ["PUMP/USD", "BTC/USD"]
    )
    # Max coins scored per cycle (each costs one get_bars call). The bot scans
    # the vetted basket first; if you raise this it will also probe the broader
    # Kraken USD market (via list_usd_pairs) up to the limit. Keep small to
    # respect Kraken's public rate limits.
    universe_scan_limit: int = Field(default=12, ge=1)
    # ── Self-learning agent ──────────────────────────────────────
    # When enabled, the bot records every closed trade's P&L keyed by coin +
    # regime and biases coin selection toward coins with proven positive
    # expectancy (and away from bleeders). This closes the loop that the
    # reporting-only learning.py leaves open.
    learner_enabled: bool = Field(default=True)
    # Minimum trades before a coin's learned expectancy is trusted enough to
    # bias selection (avoids over-fitting to a single lucky/unlucky fill).
    learner_min_trades: int = Field(default=3, ge=1)
    learner_path: Path = Path("logs/learner.json")
    # Adaptive gate (learner.gate): per-symbol / per-regime rolling expectancy
    # (net bps after fees) blended with walk-forward OOS priors. A symbol or
    # regime with negative live expectancy over >= learner_min_sample closed
    # trades is benched for learner_bench_hours, then gets one probation trade
    # at reduced size. Weak/negative-but-unproven names trade at reduced size.
    learner_priors_path: Path = Path("data/walkforward_results.json")
    # No bench before 30 closed trades per strategy+coin (audit policy). Values
    # below the floor (e.g. an old LEARNER_MIN_SAMPLE=8 in an env file) are raised.
    learner_min_sample: int = Field(default=30, ge=2)
    learner_bench_hours: float = Field(default=72.0, gt=0)
    learner_gate_enabled: bool = Field(default=True)
    # Paper/dry-run sizing from the paper ledger (cash + open cost basis,
    # seeded at STRATEGY_EQUITY_USD) instead of the real Kraken balance.
    # Default ON: paper mode must never need the private Balance endpoint.
    paper_use_ledger_equity: bool = Field(default=True)
    # Paper/dry-run never calls PRIVATE Kraken endpoints (Balance, TradesHistory,
    # OpenOrders, ...). Two copies polling private endpoints with one key caused
    # 1,511 "EGeneral:Temporary lockout" errors in the legacy box run.
    paper_block_private_api: bool = Field(default=True)
    # ── Regime-switch trend sleeve (STRATEGY=regime_trend) ─────
    regime_lookback: int = Field(default=20, ge=2)
    regime_atr_mult: float = Field(default=3.0, gt=0)
    regime_min_atr_rank: float = Field(default=0.0, ge=0.0, le=1.0)
    # Optional order-flow entry filter (technicals.FLOW_FILTERS, e.g. "ofi_pos").
    # Empty = off. Only set after it passes the walk-forward promotion rules in
    # docs/PIPELINE.md; fails closed when bars carry no tick order flow.
    regime_flow_filter: str = Field(default="")
    # ── 4h mean-reversion sleeve (PAPER ONLY, second sleeve) ──────
    # Runs in the same paper loop as the primary (e.g. regime_trend@1h)
    # sleeve. Parameters are the walk-forward default spec that was selected
    # in every fold for BTC/ETH/SOL at 240m ("meanrev_mk": RSI<=38 & close <
    # EMA50, resting limit 0.1% under the signal close valid for ONE bar, exit
    # RSI>=55 or close>=EMA50, 3% stop, 25% TP). See docs/MEANREV_SLEEVE.md.
    # The sleeve never submits exchange orders; it is disabled whenever any
    # safety lock is off (paper_trading/dry_run/allow_live_trading).
    meanrev_sleeve_enabled: bool = Field(default=True)
    meanrev_symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"]
    )
    meanrev_timeframe_minutes: int = Field(default=240, ge=15)
    meanrev_rsi_entry: float = Field(default=38.0, gt=0, lt=100)
    meanrev_rsi_exit: float = Field(default=55.0, gt=0, lt=100)
    meanrev_ema_period: int = Field(default=50, ge=5)
    meanrev_stop_pct: float = Field(default=0.03, gt=0, le=0.2)
    meanrev_take_profit_pct: float = Field(default=0.25, gt=0, le=1.0)
    meanrev_limit_offset_pct: float = Field(default=0.001, ge=0.0, le=0.02)
    # Paper limit order lifetime in bars of meanrev_timeframe_minutes. An
    # unfilled limit EXPIRES (it never converts to a market order).
    meanrev_limit_valid_bars: int = Field(default=1, ge=1, le=6)
    # Optional order-flow entry filter for the 4h sleeve (see regime_flow_filter).
    meanrev_flow_filter: str = Field(default="")
    meanrev_state_path: Path = Path("logs/meanrev_sleeve.json")
    # ── Daily risk-on filter (D1) on ENTRIES ──────────────────────
    # close_d > SMA50_d and SMA50_d rising over 5 days, closed UTC daily bars
    # (Kraken public daily OHLC). Gates entries only, never forces an exit;
    # unknown daily data blocks entries (fail closed). ON by default: the
    # pre-registered walk-forward (docs/PIPELINE.md, data/d1_trendhold_report.txt)
    # did not find it hurting out of sample.
    regime_daily_filter: bool = Field(default=True)
    meanrev_daily_filter: bool = Field(default=True)
    # ── 4h trend-hold PAPER sleeve (third sleeve) ─────────────────
    # Entry after a 4h close with D1 on, close > EMA100 and EMA20 > EMA100
    # (market, next cycle); exit at the first 4h close below EMA100. No hard
    # stop. Fixed 25% of the paper book per coin; one position per coin across
    # ALL sleeves (SleeveRegistry); total exposure capped by
    # max_exposure_fraction (0.75 in run_paper_mac.sh). Paper only.
    trendhold_sleeve_enabled: bool = Field(default=True)
    trendhold_timeframe_minutes: int = Field(default=240, ge=60)
    trendhold_symbols: list[str] = Field(default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"])
    trendhold_position_fraction: float = Field(default=0.25, gt=0, le=0.34)
    trendhold_ema_fast: int = Field(default=20, ge=2)
    trendhold_ema_slow: int = Field(default=100, ge=10)
    trendhold_daily_filter: bool = Field(default=True)
    trendhold_state_path: Path = Path("logs/trendhold_sleeve.json")
    # ── Market-data pipeline (Data -> Ticks -> Bars -> Technicals) ──
    # When enabled, gateway.get_bars() for BTC/ETH/SOL at 1/15/60/240m serves
    # tick-built bars (with order-flow columns) from PIPELINE_DATA_DIR, written
    # by scripts/tick_collector.py, and stitches Kraken REST OHLC in for any
    # missing/short/stale history. Off by default (tests and other deploys
    # keep the plain REST path). See docs/PIPELINE.md. Read-only market data;
    # no effect on order routing or the safety locks.
    pipeline_enabled: bool = Field(default=False)
    pipeline_data_dir: Path = Path("data")
    pipeline_symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"]
    )
    pipeline_stale_seconds: float = Field(default=600.0, gt=0)
    # ── Sentiment agent (Stage 1) ──────────────────────────────
    # When enabled, live news/Reddit sentiment acts as a confirmation filter:
    # bearish mood blocks fresh BUYs, a collapse forces a protective SELL. It
    # never originates a trade on its own.
    sentiment_enabled: bool = Field(default=True)
    # ── Active signal strategy ─────────────────────────────────
    # "momentum" (default): RSI band + HARD regime EMA gate (no counter-trend
    #   BUYs). In-position exits (sell below slow EMA) unchanged.
    # "mean_reversion": oversold stretch + reversion exit.
    # "sr_flip": resistance→support (and reverse) pivot-flip sleeve.
    # "pattern" / "elliott_lite": rule-based double-bottom / bullish-flag.
    # "breakout" / "momentum_breakout": N-bar high + volume > avg (defined-risk
    #   2% stop / 4% TP; ADX sit-out soft-disabled — volume+high break is the
    #   gate; fee min-edge still applies and 4% TP clears MIN_EDGE_BPS=100).
    # Safety (budget cap, exchange stop, drawdown/daily-loss breakers) is
    # unchanged — aggression is in entry frequency, not in risk.
    # Default (and the only paper/live choice): regime_trend on >= 1h bars.
    # momentum, every sub-1h sleeve and the other legacy strategies are
    # RETIRED from paper/live (they lost after fees in every window); they stay
    # importable for backtests only and need ALLOW_RETIRED_STRATEGIES=true.
    strategy: str = Field(default="regime_trend")
    allow_retired_strategies: bool = Field(default=False)
    # ── Momentum breakout sleeve knobs (STRATEGY=breakout) ─────
    # Universe scanned when strategy is breakout / momentum_breakout.
    # Map BTC/USD→XXBTZUSD etc. via the Kraken gateway (canonical form here).
    breakout_symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"]
    )
    # Volume average window (env BREAKOUT_VOL_AVG); lookback uses BREAKOUT_LOOKBACK.
    breakout_vol_avg: int = Field(default=20, ge=2)
    # Max open lots across the breakout universe (1 per symbol still enforced).
    max_concurrent_positions: int = Field(default=3, ge=1, le=10)
    # ── S/R flip knobs (strategy=sr_flip) ───────────────────────
    sr_lookback: int = Field(default=120, ge=40)
    sr_pivot_strength: int = Field(default=3, ge=1, le=10)
    sr_min_break_atr: float = Field(default=0.25, ge=0.0)
    sr_min_break_pct: float = Field(default=0.002, ge=0.0)  # 0.2%
    # ── Pattern / elliott_lite knobs ───────────────────────────
    pattern_pivot_strength: int = Field(default=2, ge=1, le=8)
    pattern_dbl_tol_pct: float = Field(default=0.02, gt=0, le=0.1)  # bottoms within 2%
    pattern_min_break_atr: float = Field(default=0.0, ge=0.0)
    pattern_min_break_pct: float = Field(default=0.001, ge=0.0)  # 0.1%
    pattern_flag_pole_pct: float = Field(default=0.03, gt=0)  # 3% pole
    pattern_flag_pole_bars: int = Field(default=8, ge=3)
    pattern_flag_bars: int = Field(default=6, ge=3)
    pattern_flag_max_range_pct: float = Field(default=0.025, gt=0)  # tight flag
    # ── Position rotation (aggressive, single-position) ────────
    # When the bot holds a position on one coin but a DIFFERENT allowed coin
    # shows a clearly stronger momentum setup, it rotates: sells the weaker
    # bot-owned lot and enters the stronger. This is what lets the bot "trade
    # all things" (e.g. rotate PUMP -> BTC) instead of sitting in one coin
    # forever. The exit always sells only the bot's own accumulated lot, and
    # the new entry still runs through every risk gate. Turn off to keep the
    # bot pinned to whichever coin it is currently holding.
    rotate_positions: bool = Field(default=True)
    rotate_min_score_gap: float = Field(default=15.0, ge=0.0)
    dca_enabled: bool = Field(default=False)
    dca_symbol: str = Field(default="PUMP/USD")
    dca_interval_minutes: int = Field(default=240, ge=30)
    dca_fixed_usd: float = Field(default=12.0, gt=0)
    dca_max_buys_per_day: int = Field(default=4, ge=0)
    dca_max_total_buys: int = Field(default=40, ge=0)
    dca_stop_buffer: float = Field(default=0.05, gt=0, le=0.5)  # synthetic stop for risk gate only
    dca_state_path: Path = Path("logs/dca_state.json")  # persisted interval/cap counters
    # Legacy compatibility setting only. Kraken execution is permanently spot-only;
    # the gateway strips leverage from every submitted order.
    margin_enabled: bool = Field(default=False)
    # Conservative defaults ported from the Dublin repo (sorrymoney010/Dublin-).
    # max_leverage is NOT exchange leverage. It is only the cap the vol-target
    # sizer (dublin_bot.sizing) applies to its vol scale and target notional
    # (notional <= equity * max_leverage). 1.0 = never size above 1x equity,
    # which matches spot-only execution.
    max_leverage: float = Field(default=1.0, gt=0, le=5.0)
    # Only read by RiskManager when margin_enabled=True (legacy; margin is
    # never used on Kraken). Default 0 so enabling margin by accident caps
    # exposure at zero instead of silently allowing 25%.
    margin_exposure_fraction: float = Field(default=0.0, ge=0, le=0.5)
    # RISK_PCT is kept as an alias for RISK_PER_TRADE.
    risk_per_trade: float = Field(
        default=0.01,
        gt=0,
        le=0.02,
        validation_alias=AliasChoices("RISK_PER_TRADE", "RISK_PCT"),
    )
    max_position_fraction: float = Field(default=0.25, gt=0, le=0.5)
    # Vol-target / fractional-Kelly sizing (see dublin_bot.sizing).
    target_vol: float = Field(default=0.12, gt=0, le=1.0)
    kelly_fraction: float = Field(default=0.25, gt=0, le=0.5)
    max_exposure_fraction: float = Field(default=0.5, gt=0, le=1.0)
    max_daily_loss_fraction: float = Field(default=0.03, gt=0, le=0.05)
    max_drawdown_fraction: float = Field(default=0.10, gt=0, le=0.20)
    # Daily order cap. Set to 0 for unlimited orders per day (cooldown still
    # applies between entries). Bounded at 10 when a cap is used.
    max_orders_per_day: int = Field(default=0, ge=0, le=10)
    cooldown_minutes: int = Field(default=10, ge=0)
    monitor_interval_seconds: int = Field(default=900, ge=60, le=86400)
    candle_close_delay_seconds: int = Field(default=2, ge=1, le=30)
    # Dashboard startup is observational by default. An operator must press
    # Start (or explicitly opt in here) before automated cycles may run.
    auto_start_monitor: bool = Field(default=False)
    # Rapid mode flag (status only). Does not loosen strategy RSI/momentum or
    # any risk/loss/exposure threshold — it only selects the faster cadence above.
    rapid_mode: bool = Field(default=True)

    fast_ema: int = Field(default=20, ge=2)
    slow_ema: int = Field(default=50, ge=3)
    regime_ema: int = Field(default=200, ge=10)
    rsi_period: int = Field(default=14, ge=2)
    rsi_min: float = 35.0   # wider band so momentum fires in normal (non-extreme) conditions
    rsi_max: float = 75.0   # — aggressive entry frequency, still excludes overbought/washed-out
    rsi_oversold: float = 38.0   # looser MR entry so mild dips also trigger (aggressive)
    rsi_exit: float = 55.0       # mean-reversion exit threshold (recovered)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=1.5, gt=0)
    # ── ADX regime sit-out (momentum) ──────────────────────
    # Wilder ADX on engine OHLCV bars. HARD gate for momentum BUYs:
    # trade only when ADX > adx_enter_above (~25); sit out when ADX <
    # adx_exit_below (~20); hysteresis keeps prior allow/deny between.
    adx_period: int = Field(default=14, ge=2)
    adx_enter_above: float = Field(default=25.0, gt=0)
    adx_exit_below: float = Field(default=20.0, gt=0)
    adx_gate_enabled: bool = Field(default=True)
    # ── Fee-aware minimum edge ─────────────────────────────
    # Kraken Tier 1 ~40 bps maker / 80 bps taker → RT taker ~160 bps.
    # Block new BUYs whose TP / expected move is below this floor
    # (100–150 bps recommended) so micro targets cannot clear fees.
    min_edge_bps: float = Field(default=100.0, ge=0.0)
    min_edge_gate_enabled: bool = Field(default=True)
    breakout_lookback: int = Field(default=20, ge=2)
    volume_lookback: int = Field(default=20, ge=2)
    min_volume_ratio: float = Field(default=1.10, gt=0)
    min_order_notional_usd: float = Field(default=1.0, ge=1.0)
    # ── Fill-model cost assumptions (backtest + paper) ──────
    # These MUST mirror the real exchange fee schedule or every backtest is
    # fiction. Kraken Pro tier-1 (< $10k 30d volume): 40 bps taker / 25 bps
    # maker, plus 10 bps assumed slippage on taker fills. Same values as
    # run_paper_mac.sh, the walk-forward and the studies.
    # Override via env (PAPER_TAKER_FEE_BPS etc.); `TradeVolume` is the
    # authoritative source.
    paper_taker_fee_bps: float = Field(default=40.0, ge=0.0)
    paper_maker_fee_bps: float = Field(default=25.0, ge=0.0)
    paper_slippage_bps: float = Field(default=10.0, ge=0.0)
    paper_min_spread_bps: float = Field(default=5.0, ge=0.0)
    # ── Advanced order execution (Kraken full API) ──────────
    # The bot places exchange-native bracket orders (stop-loss + take-profit)
    # and can use limit (maker) entries to cut fees. All still flow through the
    # idempotency, precision, and risk gates.
    order_type: str = Field(default="market", pattern="^(market|limit)$")
    # Fraction of ask (buy) / bid (sell) used to post a limit order inside the
    # spread. 0.001 = 0.1% better than touch. Only used when order_type="limit".
    limit_offset_pct: float = Field(default=0.001, gt=0, le=0.02)
    use_bracket: bool = Field(default=True)  # attach SL+TP on entry
    # Paper/live protective stop & TP fractions. Breakout sleeve: set
    # STOP_LOSS_PCT=0.02 (or STOP_PCT) and TAKE_PROFIT_PCT=0.04 for 2R.
    stop_loss_pct: float = Field(
        default=0.04,
        gt=0,
        le=0.50,
        validation_alias=AliasChoices("STOP_LOSS_PCT", "STOP_PCT"),
    )
    take_profit_pct: float = Field(
        default=0.08,
        gt=0,
        le=1.0,
        validation_alias=AliasChoices("TAKE_PROFIT_PCT", "TP_PCT"),
    )
    trailing_stop: bool = Field(default=False)                 # legacy trail flag; sleeves use the fields below
    # ATR trailing take-profit. Each sleeve stays OFF until a walk-forward
    # (scripts/backtest_trailing.py) shows the overlay improves out-of-sample
    # results after fees. See docs/PAPER_PRO.md.
    trailing_tp_regime: bool = Field(default=False)
    trailing_tp_meanrev: bool = Field(default=False)
    trailing_tp_trendhold: bool = Field(default=False)
    trailing_tp_futures: bool = Field(default=False)
    trailing_activate_atr: float = Field(default=1.5, gt=0)
    trailing_atr_mult: float = Field(default=1.0, gt=0)
    trailing_state_path: Path = Path("logs/trailing_state.json")
    # Fast paper exits (stops / trailing / take-profit) between the 300s loop.
    # Public prices only. Does not place entries. Still requires the ledger
    # owner and runs inside the single-instance lock.
    exit_watcher_enabled: bool = Field(default=True)
    # Perpetual short sleeve. OFF until scripts/backtest_futures_short.py
    # clears the promotion bar. Leverage cannot be set above 2.
    futures_sleeve_enabled: bool = Field(default=False)
    futures_leverage: float = Field(default=1.0, gt=0, le=2.0)
    futures_maintenance_margin: float = Field(default=0.01, gt=0, lt=0.5)
    futures_timeframe_minutes: int = Field(default=240, ge=60)
    futures_symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"]
    )
    futures_ema_fast: int = Field(default=20, ge=2)
    futures_ema_slow: int = Field(default=100, ge=10)
    futures_margin_fraction: float = Field(default=0.25, gt=0, le=0.34)
    futures_ledger_path: Path = Path("logs/paper_futures.json")
    # SHADOW perp sleeves (signals + virtual P&L ledger only; no paper fills, no
    # orders, no lock changes). OFF by default. See reports/perp_study_2026-10-10.md
    # and src/dublin_bot/futures_shadow.py.
    futures_shadow_d1flip_enabled: bool = Field(default=False)
    futures_shadow_d1flip_bear: str = Field(default="strict", pattern="^(strict|repo)$")
    futures_shadow_d1flip_leverage: float = Field(default=1.0, gt=0, le=2.0)
    futures_shadow_donchian_enabled: bool = Field(default=False)
    futures_shadow_donchian_entry_days: int = Field(default=55, ge=5, le=120)
    futures_shadow_donchian_exit_days: int = Field(default=20, ge=2, le=60)
    futures_shadow_donchian_leverage: float = Field(default=1.0, gt=0, le=2.0)
    futures_shadow_book_usd: float = Field(default=500.0, gt=0)
    futures_shadow_symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "SOL/USD"]
    )
    futures_shadow_logs_dir: Path = Path("logs")
    futures_shadow_budget_seconds: float = Field(default=3.0, ge=0.0, le=30.0)
    # Live order locks for venues the paper bot only simulates. Independent of
    # PAPER_TRADING / DRY_RUN / ALLOW_LIVE_TRADING and default OFF. A real
    # order also requires every existing lock to be open. See venue_locks.py.
    allow_futures_live_orders: bool = Field(default=False)
    allow_margin_live_orders: bool = Field(default=False)
    journal_path: Path = Path("logs/decisions.jsonl")

    @field_validator("learner_min_sample", mode="after")
    @classmethod
    def _learner_floor(cls, v: int) -> int:
        return max(int(v), 30)

    @model_validator(mode="after")
    def validate_safety(self) -> "Settings":
        if not self.paper_trading:
            if not self.allow_live_trading:
                raise ValueError("Live mode blocked: ALLOW_LIVE_TRADING must be true")
            if self.live_risk_acknowledgement != "I_ACCEPT_LIVE_TRADING_RISK":
                raise ValueError("Live mode blocked: acknowledgement is missing")
        if not (self.fast_ema < self.slow_ema < self.regime_ema):
            raise ValueError("EMA periods must satisfy fast < slow < regime")
        if self.rsi_min >= self.rsi_max:
            raise ValueError("RSI_MIN must be below RSI_MAX")
        if self.adx_enter_above <= self.adx_exit_below:
            raise ValueError("ADX_ENTER_ABOVE must be greater than ADX_EXIT_BELOW")
        return self

    @property
    def has_credentials(self) -> bool:
        return bool(self.kraken_api_key and self.kraken_api_secret)

    @property
    def allowed_symbols(self) -> list[str]:
        """Deduplicated safety basket.

        In ``basket`` universe mode this is the fixed ``coin_basket``. In
        ``all_usd`` mode the engine discovers the full Kraken market at runtime
        via the gateway, so this property is only used as a fallback / safety
        reference, not the live universe.
        """
        ordered: list[str] = []
        for sym in self.coin_basket:
            sym = str(sym).strip().upper()
            if sym and sym not in ordered:
                ordered.append(sym)
        return ordered

    @property
    def safety_locked(self) -> bool:
        """True when all three independent locks forbid real-money execution."""
        return self.paper_trading and self.dry_run and not self.allow_live_trading

    @property
    def active_mode(self) -> str:
        """Human-readable active execution mode: 'live' or 'paper'."""
        if self.allow_live_trading and not self.paper_trading and not self.dry_run:
            return "live"
        return "paper"

    def safety_report(self) -> dict[str, object]:
        """Credential-free summary suitable for logs, audit, and the dashboard."""
        return {
            "safety_locked": self.safety_locked,
            "paper_trading": self.paper_trading,
            "dry_run": self.dry_run,
            "allow_live_trading": self.allow_live_trading,
            "live_risk_acknowledgement_present": bool(self.live_risk_acknowledgement),
            "broker": self.broker,
            "credentials_present": self.has_credentials,
            "symbol": self.symbol,
            "strategy_equity_usd": self.strategy_equity_usd,
            "max_orders_per_day": self.max_orders_per_day,
        }


# ── Paper/live strategy choices ──────────────────────────────────
# Only these strategies may run in the paper or live loop. Everything else
# (momentum, breakout@5m/15m, sr_flip, pattern, mean_reversion, ...) is
# backtest-only. Timeframes below 60 minutes are retired for every sleeve.
PAPER_LIVE_STRATEGIES = frozenset({"regime_trend", "regime"})
MIN_PAPER_LIVE_TF_MINUTES = 60


def paper_live_choice_ok(settings: "Settings") -> tuple[bool, str]:
    """Is this configuration an allowed paper/live choice? (never raises)"""
    if getattr(settings, "allow_retired_strategies", False):
        return True, "retired strategies allowed (backtest/research override)"
    name = str(getattr(settings, "strategy", "") or "").lower()
    if name not in PAPER_LIVE_STRATEGIES:
        return False, (f"strategy '{name}' is retired from paper/live "
                       f"(allowed: {sorted(PAPER_LIVE_STRATEGIES)}; backtest-only otherwise)")
    for field_name in ("timeframe_minutes", "meanrev_timeframe_minutes", "trendhold_timeframe_minutes"):
        tf = getattr(settings, field_name, None)
        if tf is not None and int(tf) < MIN_PAPER_LIVE_TF_MINUTES:
            return False, f"{field_name}={tf} is a retired sub-1h timeframe"
    if getattr(settings, "futures_sleeve_enabled", False):
        tf = int(getattr(settings, "futures_timeframe_minutes", 240) or 240)
        if tf < MIN_PAPER_LIVE_TF_MINUTES:
            return False, f"futures_timeframe_minutes={tf} is a retired sub-1h timeframe"
    return True, "ok"
