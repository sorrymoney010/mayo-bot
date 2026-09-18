from __future__ import annotations

from pathlib import Path
from pydantic import Field, model_validator
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
    # When the real account balance is too small to trade the configured symbol
    # at the minimum notional, automatically fall back to a cheaper allowed coin.
    auto_cheaper_symbol: bool = Field(default=True)
    fallback_symbols: list[str] = Field(default_factory=lambda: ["PUMP/USD", "XRP/USD", "UNI/USD", "BTC/USD"])
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
    # ── Sentiment agent (Stage 1) ──────────────────────────────
    # When enabled, live news/Reddit sentiment acts as a confirmation filter:
    # bearish mood blocks fresh BUYs, a collapse forces a protective SELL. It
    # never originates a trade on its own.
    sentiment_enabled: bool = Field(default=True)
    # ── Active signal strategy ─────────────────────────────────
    # "momentum" (default, RSI band gate) or "mean_reversion" (oversold
    # stretch + reversion exit). momentum is the more active engine — it takes
    # setups whenever RSI is inside the tradable band, so the bot trades far
    # more often (aggressive) rather than waiting for a washed-out dip.
    # Safety (budget cap, exchange stop, drawdown/daily-loss breakers) is
    # unchanged — aggression is in entry frequency, not in risk.
    strategy: str = Field(default="momentum")
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
    max_leverage: float = Field(default=2.0, gt=0, le=5.0)
    margin_exposure_fraction: float = Field(default=0.25, gt=0, le=0.5)
    risk_per_trade: float = Field(default=0.02, gt=0, le=0.02)
    max_position_fraction: float = Field(default=0.40, gt=0, le=0.5)
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
    breakout_lookback: int = Field(default=20, ge=2)
    volume_lookback: int = Field(default=20, ge=2)
    min_volume_ratio: float = Field(default=1.10, gt=0)
    min_order_notional_usd: float = Field(default=1.0, ge=1.0)
    # ── Fill-model cost assumptions (backtest + paper) ──────
    # These MUST mirror the real exchange fee schedule or every backtest is
    # fiction. Kraken's "starter" tier on a ~$2.3k 30d volume account charges
    # 80 bps taker / 40 bps maker — the old hardcoded 26 bps understated a
    # round trip by ~1.1% and made losing strategies look profitable.
    # Override via env (PAPER_TAKER_FEE_BPS etc.) once your tier improves;
    # `TradeVolume` is the authoritative source.
    paper_taker_fee_bps: float = Field(default=80.0, ge=0.0)
    paper_maker_fee_bps: float = Field(default=40.0, ge=0.0)
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
    stop_loss_pct: float = Field(default=0.04, gt=0, le=0.50)   # 4% below entry
    take_profit_pct: float = Field(default=0.08, gt=0, le=1.0)  # 8% above entry
    trailing_stop: bool = Field(default=False)                 # trail SL to peak
    journal_path: Path = Path("logs/decisions.jsonl")

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
