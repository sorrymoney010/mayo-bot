"""
Rotation Strategy for fast, multi-coin momentum trading.

Rules:
- Scan ALL allowed coins every cycle
- Buy when momentum turns up (RSI rising + price starting to move)
- Exit on: profit target reached, stop loss hit, or momentum dying
- After any exit, force rotation to a DIFFERENT coin (no same-coin re-entry)
- Per-symbol post-sell cooldown prevents buy/sell loops
- Loop detection: if 5+ trades in 5 minutes with ~$0 PnL, stop

Not magic. Just rules. The bot follows the rules, doesn't guess.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from dublin_bot.config import Settings
from dublin_bot.models import Action, Signal


@dataclass
class RotationSetup:
    """A detected entry opportunity across one coin."""
    symbol: str
    score: float
    price: float
    rsi: float
    reason: str
    stop_price: float
    atr: float


class RotationStrategy:
    """
    Momentum rotation: find the best momentum setup across all allowed coins,
    buy it, track it, exit on profit/loss/reversal, then find the NEXT coin.

    Key differences from mean_reversion:
    - Scans ALL coins, picks the best setup (not just current symbol)
    - Buys when momentum TURNS up (faster than waiting for oversold)
    - Exits on profit target (not waiting for perfect reversal)
    - AFTER SELL: blocks same symbol for post_sell_cooldown minutes
    - Forces rotation to a DIFFERENT coin after every exit
    - Designed for "boom boom boom" action across multiple coins
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._candidate: Optional[str] = None
        self._position: Optional[str] = None  # confirmed coin we hold
        self._entry_price: Optional[float] = None
        self._entry_time: Optional[float] = None
        self._quantity: float = 0.0
        # Loop prevention: track last exit time per symbol
        self._last_exit_time: dict[str, float] = {}  # symbol -> unix timestamp
        self._post_sell_cooldown: int = 3  # minutes; 3*60=180s cooldown per symbol after sell
        self._paper_state_path = Path(
            getattr(settings, "session_state_path", "logs/session_state.json")
        ).with_name("rotation_paper_state.json")
        # Paper state is never loaded as live ownership. Corrupt state raises;
        # silently treating unreadable ownership as flat could duplicate entries.
        if (settings.paper_trading or settings.dry_run) and self._paper_state_path.exists():
            data = json.loads(self._paper_state_path.read_text())
            if data["version"] != 1:
                raise ValueError("Unsupported rotation paper state version")
            self._position = data["position"]
            self._entry_price = data["entry_price"]
            self._quantity = float(data["quantity"])
            if not math.isfinite(self._quantity) or (
                self._quantity <= 0 if self._position else self._quantity != 0
            ):
                raise ValueError("Invalid persisted rotation quantity")
            self._entry_time = data["entry_time"]
            self._last_exit_time = data["last_exit_time"]

    def _save_paper_state(self):
        if not (self.settings.paper_trading or self.settings.dry_run):
            return
        data = {
            "version": 1, "position": self._position,
            "entry_price": self._entry_price, "quantity": self._quantity,
            "entry_time": self._entry_time, "last_exit_time": self._last_exit_time,
        }
        self._paper_state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._paper_state_path.parent, prefix=".rotation.")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self._paper_state_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def reset(self):
        """Clear position state (for paper trading reset, new session, etc.)"""
        self._position = None
        self._entry_price = None
        self._entry_time = None
        self._last_exit_time = {}
        self._candidate = None
        self._quantity = 0.0
        self._save_paper_state()

    def get_position(self) -> Optional[str]:
        """Return the symbol we currently hold, or None."""
        return self._position

    def get_candidate(self) -> Optional[str]:
        """Selected entry, not evidence of a fill or ownership."""
        return self._candidate

    def last_trade_time(self) -> float:
        return max([self._entry_time or 0.0, *self._last_exit_time.values()])

    def get_entry_price(self) -> Optional[float]:
        return self._entry_price

    def evaluate(self, bars: object, in_position: bool = False) -> Signal:
        """
        Evaluate a single coin's bars and return a trading signal.

        Args:
            bars: pandas DataFrame with OHLCV data (must have 'close', 'volume' columns)
            in_position: whether we currently hold this coin

        Returns:
            Signal with BUY/SELL/WAIT action
        """
        import pandas as pd
        from dublin_bot.models import Action

        if bars is None or len(bars) < 20:
            return Signal(Action.WAIT, 0, "Insufficient data", price=0.0)

        # Get current price
        current_price = float(bars["close"].iloc[-1])

        # Calculate RSI
        delta = bars["close"].diff()
        gain = delta.clip(lower=0).rolling(window=self.settings.rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(window=self.settings.rsi_period).mean()
        rs = gain / loss.replace(0, 1)
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])

        # Calculate momentum (price change over lookback)
        lookback = min(self.settings.lookback_bars, len(bars))
        if lookback < 5:
            return Signal(Action.WAIT, 0, "Not enough bars for momentum", price=current_price)

        price_change_pct = (bars["close"].iloc[-1] - bars["close"].iloc[-lookback]) / bars["close"].iloc[-lookback]

        # Calculate volume trend
        avg_volume = bars["volume"].iloc[-lookback:].mean()
        current_volume = bars["volume"].iloc[-1]
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

        # Determine action
        if in_position:
            # We hold this coin - check for exit signals
            # Use entry_price for actual PnL calculation
            if self._entry_price and self._entry_price > 0:
                entry_gain = (current_price - self._entry_price) / self._entry_price
            else:
                entry_gain = 0.0

            # Exit on profit target (3%)
            if entry_gain >= 0.03:
                return Signal(Action.SELL, 90, f"Profit target hit: +{entry_gain*100:.1f}%", price=current_price)
            # Exit on stop loss (2%)
            if entry_gain <= -0.02:
                return Signal(Action.SELL, 95, f"Stop loss hit: {entry_gain*100:.1f}%", price=current_price)
            # Exit on momentum reversal (RSI dropping below 40 after being high)
            if current_rsi < 40 and rsi.iloc[-2] > 50:
                return Signal(Action.SELL, 70, f"Momentum reversing (RSI {current_rsi:.1f})", price=current_price)
            # Exit on volume drying up
            if volume_ratio < 0.5:
                return Signal(Action.SELL, 60, f"Volume drying up (ratio {volume_ratio:.2f})", price=current_price)
            # Hold
            return Signal(Action.WAIT, 50, f"Holding, gain={entry_gain*100:.1f}%, RSI={current_rsi:.1f}", price=current_price)
        else:
            # We don't hold - check for entry signals
            # Entry: momentum-based rotation entry
            # Buy when momentum is positive (price moving up), regardless of RSI level
            # This is for "boom boom boom" - chase the momentum
            if price_change_pct > 0.005:  # At least 0.5% up over lookback
                volume_boost = min(20, (volume_ratio - 1.0) * 10)  # Volume surge bonus
                momentum_score = min(50, price_change_pct * 100 * 2)  # Up to 50 points for momentum
                rsi_score = max(0, 30 - abs(current_rsi - 55))  # Best around RSI 55
                score = int(momentum_score + volume_boost + rsi_score + 30)  # Base 30
                score = min(95, score)  # Cap at 95

                reason_parts = [f"Momentum: +{price_change_pct*100:.2f}%"]
                if volume_ratio > 1.5:
                    reason_parts.append(f"vol {volume_ratio:.2f}x")
                if current_rsi < 70:
                    reason_parts.append(f"RSI {current_rsi:.1f}")

                return Signal(Action.BUY, score,
                             ", ".join(reason_parts),
                             price=current_price,
                             stop_price=round(current_price * (1 - self.settings.stop_loss_pct), 8))

            # If momentum is slightly negative but RSI is low, might be a dip buy
            if current_rsi < 40 and price_change_pct > -0.02:
                return Signal(Action.BUY, 35,
                             f"Oversold RSI {current_rsi:.1f}, slight dip",
                             price=current_price,
                             stop_price=round(current_price * (1 - self.settings.stop_loss_pct), 8))

            # No setup
            return Signal(Action.WAIT, 5,
                         f"Waiting: RSI={current_rsi:.1f}, mom={price_change_pct*100:.2f}%, vol={volume_ratio:.2f}x",
                         price=current_price)

    def evaluate_all_coins(
        self,
        symbol_signals: dict[str, Signal],
        current_holdings: dict[str, float],
    ) -> Signal:
        """
        Evaluate all coins and pick the best action.

        Rules:
        1. If we HOLD a coin and it has a SELL signal -> SELL it, record exit
        2. If we just sold a coin, check post-sell cooldown:
           - If not cooled down: block re-entry to same symbol
           - Caller must choose a DIFFERENT symbol
        3. If no position and we have cash: pick best BUY signal
           from coins that are NOT blocked by cooldown
        4. If no good buys: WAIT

        Returns:
            Signal for the best action across ALL coins
        """
        import time as _time

        now = _time.time()
        self._candidate = None

        # 1. First priority: if we HOLD a coin, decide whether to SELL it
        if self._position and self._position in symbol_signals:
            held_signal = symbol_signals[self._position]
            if held_signal.action == Action.SELL:
                # Return SELL signal. Caller (trading agent) will execute
                # the sell and then call record_exit() to clear state.
                return held_signal

        # 2. Second priority: if we have cash, find the best BUY
        if self._position is None:
            # Filter to coins with BUY signals
            buy_signals = [
                (sym, sig) for sym, sig in symbol_signals.items()
                if sig.action == Action.BUY
            ]

            if not buy_signals:
                # No good buys anywhere - wait
                return Signal(
                    Action.WAIT, 5,
                    "No momentum setups found across any coin",
                    price=0.0
                )

            # Filter out coins on post-sell cooldown (same symbol, recently sold)
            # This prevents the buy/sell loop: sell XRP -> wait 10 min -> can buy XRP again
            current_time = now
            available_buys = []
            for sym, sig in buy_signals:
                last_exit = self._last_exit_time.get(sym, 0)
                cooldown_seconds = self._post_sell_cooldown * 60
                if current_time - last_exit >= cooldown_seconds:
                    available_buys.append((sym, sig))
                # else: skip this symbol, it's on cooldown

            if not available_buys:
                # All buy signals are for symbols on cooldown
                # Wait for cooldown to expire
                blocked_symbols = [sym for sym, _ in buy_signals]
                return Signal(
                    Action.WAIT, 5,
                    f"All setups on cooldown: {', '.join(blocked_symbols)}",
                    price=0.0
                )

            # Pick the best one by score (from available, non-blocked coins)
            best_sym, best_sig = max(available_buys, key=lambda x: x[1].score)

            # Selection alone never establishes ownership.
            self._candidate = best_sym

            return best_sig

        # 3. We're holding a position and it's not telling us to sell yet
        # Check if that position's signal changed
        held_signal = symbol_signals.get(self._position)
        if held_signal and held_signal.action == Action.WAIT:
            return held_signal

        # Holding, no sell signal yet
        return Signal(
            Action.WAIT, 50,
            f"Holding {self._position}, watching for exit",
            price=self._entry_price or 0.0
        )

    def record_entry(self, symbol: str, price: float, quantity: float):
        """Commit a confirmed fill (or successful paper fill), never a signal."""
        self._position = symbol
        self._entry_price = price
        self._quantity = quantity
        self._entry_time = time.time()
        self._candidate = None
        self._save_paper_state()

    def record_exit(self, symbol: str):
        """
        Record that we just exited a position.

        Call this AFTER the trading agent confirms the sell executed.
        This clears the position state AND records the exit time for
        the per-symbol cooldown.

        This prevents the buy/sell loop:
        - Sell XRP at T=0
        - record_exit("XRP") records T=0 as last exit
        - Next cycle: evaluate_all_coins sees BUY signal for XRP
        - But last_exit_time["XRP"] = T=0, current time < T+600s
        - So XRP is blocked, bot must pick BTC or PUMP instead
        """
        if self._position != symbol:
            raise ValueError("Exit symbol does not match confirmed position")
        now = time.time()
        self._last_exit_time[symbol] = now
        self._position = None
        self._entry_price = None
        self._entry_time = None
        self._quantity = 0.0
        self._candidate = None
        self._save_paper_state()

    def get_last_exit_time(self, symbol: str) -> Optional[float]:
        """Get the last exit time for a symbol (for debugging/monitoring)."""
        return self._last_exit_time.get(symbol)

    def time_since_last_exit(self, symbol: str) -> float:
        """Seconds since last exit for a symbol. 0 if never exited."""
        last = self._last_exit_time.get(symbol, 0)
        return max(0, time.time() - last)

    def get_holdings(self) -> dict[str, float]:
        """Return what we hold. For paper trading, this is tracked internally."""
        if self._position and self._quantity > 0:
            return {self._position: self._quantity}
        return {}
