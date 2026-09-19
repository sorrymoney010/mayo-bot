"""
Monitoring Agent: watches the trading agent, confirms trades, reports wins/losses.

Runs continuously, checking every N seconds:
- Did a trade execute?
- Confirm it on the exchange (or paper ledger)
- Report: symbol, action, price, quantity, Pnl, status
- Track wins and losses separately
- Alert on problems

This is the "eyes" that make sure the trading agent isn't lying or broken.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

from dublin_bot.agents.trading_agent import TradingAgent

logger = logging.getLogger(__name__)


class MonitoringAgent:
    """
    Watches the trading agent and confirms everything.
    
    Every cycle:
    1. Check if the trading agent executed a trade
    2. Verify it against the exchange/paper ledger
    3. Report confirmed trade details
    4. Track win/loss statistics
    5. Alert on anomalies
    
    Can run as a separate thread or be polled.
    """

    def __init__(
        self,
        trading_agent: TradingAgent,
        report_callback: Optional[Callable[[dict], None]] = None,
        check_interval_seconds: int = 60,
        log_path: str = "logs/monitor_report.json",
    ):
        self.trading_agent = trading_agent
        self.report_callback = report_callback
        self.check_interval = check_interval_seconds
        self.log_path = Path(log_path)
        self._stop_event = threading.Event()
        self._check_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        
        # Tracking state
        self._last_confirmed_trade: Optional[dict] = None
        self._wins: list[dict] = []
        self._losses: list[dict] = []
        self._confirmed_trades: list[dict] = []
        self._pending_trades: dict[str, dict] = {}
        
        # Load previous state
        self._load_state()
        
    def _load_state(self):
        """Load previously confirmed trades and stats."""
        if self.log_path.exists():
            try:
                with open(self.log_path) as f:
                    data = json.load(f)
                    for trade in data.get("pending_trades", []):
                        if trade.get("order_id"):
                            self._pending_trades[trade["order_id"]] = trade
                    for trade in data.get("confirmed_trades", []):
                        if trade.get("mode") != "paper" and (
                            not trade.get("exchange_verified") or
                            trade.get("status", "").upper() == "SUBMITTED"
                        ):
                            if trade.get("order_id"):
                                self._pending_trades[trade["order_id"]] = trade
                            continue
                        self._record_confirmation(trade)
                        if trade.get("mode") == "live" and trade.get("exchange_status") == "open":
                            self._pending_trades[trade["order_id"]] = trade
            except (json.JSONDecodeError, KeyError):
                pass
    
    def _save_state(self):
        """Persist current state."""
        data = {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "confirmed_trades": self._confirmed_trades,
            "pending_trades": list(self._pending_trades.values()),
            "wins": self._wins,
            "losses": self._losses,
            "total_wins": len(self._wins),
            "total_losses": len(self._losses),
            "win_rate": self.get_stats()["win_rate"],
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.log_path.parent,
                prefix=f".{self.log_path.name}.", suffix=".tmp", delete=False,
            ) as f:
                temporary = Path(f.name)
                json.dump(data, f, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.log_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    
    def start_monitoring(self):
        """Start the monitoring loop in a background thread."""
        if self._thread and self._thread.is_alive():
            return  # Already running
        
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self._thread.start()
    
    def stop_monitoring(self):
        """Stop the monitoring loop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
    
    def _monitoring_loop(self):
        """Main monitoring loop: check every N seconds."""
        while not self._stop_event.is_set():
            self.check_and_report()
            self._stop_event.wait(self.check_interval)
    
    def check_and_report(self) -> dict:
        """Serialize monitoring and coordinator checks without adding scans."""
        with self._check_lock:
            return self._check_and_report_locked()

    def _check_and_report_locked(self) -> dict:
        """
        Check the trading agent's state, confirm trades, report.
        
        Returns a report dict with:
        - last_trade: what happened last
        - wins: list of winning trades
        - losses: list of losing trades
        - stats: win rate, total Pnl, etc.
        - anomalies: anything suspicious
        """
        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "paper_trading": self.trading_agent.settings.paper_trading,
            "last_trade": self._last_confirmed_trade,
            "pending_conflicts": [],
            "anomalies": [],
            "stats": self.get_stats(),
        }
        
        # Get the trading agent's latest report
        try:
            agent_report = self.trading_agent.scan_and_decide()
            report["agent_decision"] = agent_report
        except Exception as e:
            report["anomalies"].append(f"Agent report failed: {e}")
            agent_report = {}
        
        # Re-query pending orders once per cycle, independently of new decisions.
        candidates = dict(self._pending_trades)
        if agent_report.get("executed") and agent_report.get("order_result"):
            result = agent_report["order_result"]
            candidates[result.get("order_id") or "paper"] = result
        for result in candidates.values():
            try:
                confirmed_trade = self._confirm_trade(result, agent_report)
            except Exception as exc:
                report["anomalies"].append(f"Trade verification failed ({type(exc).__name__})")
                continue
            previous_trade = self._last_confirmed_trade
            distinct = confirmed_trade and not any(
                (confirmed_trade.get("order_id") and t.get("order_id") == confirmed_trade["order_id"])
                or t == confirmed_trade for t in self._confirmed_trades
            )
            if confirmed_trade and self._record_confirmation(confirmed_trade):
                if distinct:
                    report["anomalies"].extend(self._rapid_trade_anomalies(previous_trade, confirmed_trade))
                report["last_confirmed_trade"] = confirmed_trade
                if self.report_callback:
                    try:
                        self.report_callback(confirmed_trade)
                    except Exception as exc:
                        logger.error("Trade callback failed (%s)", type(exc).__name__)
        try:
            self._save_state()
        except Exception as exc:
            # Preserve the previous on-disk snapshot and current in-memory
            # evidence; a later cycle can retry persistence without a new scan.
            report["anomalies"].append(f"Monitor persistence failed ({type(exc).__name__})")
        report["pending_trades"] = list(self._pending_trades.values())
        report["last_trade"] = self._last_confirmed_trade
        report["stats"] = self.get_stats()

        # Check for anomalies
        anomalies = self._check_anomalies(agent_report)
        report["anomalies"].extend(anomalies)
        report["anomalies"].extend(
            f"Order {order_id}: {pending['verification_error']}"
            for order_id, pending in self._pending_trades.items()
            if pending.get("verification_error")
        )
        
        # If callback provided, send full report
        if self.report_callback:
            try:
                self.report_callback(report)
            except Exception as exc:
                logger.error("Report callback failed (%s)", type(exc).__name__)
        
        return report
    
    def _record_confirmation(self, trade: dict) -> bool:
        """Upsert cumulative exchange execution, never count an order twice."""
        old = next((t for t in self._confirmed_trades
                    if trade.get("order_id") and t.get("order_id") == trade["order_id"]), None)
        if old:
            # Ignore observation timestamps when detecting a changed execution.
            if all(old.get(k) == v for k, v in trade.items() if k != "timestamp"):
                return False
            self._confirmed_trades[self._confirmed_trades.index(old)] = trade
        elif trade not in self._confirmed_trades:
            self._confirmed_trades.append(trade)
        else:
            return False
        self._last_confirmed_trade = trade
        self._wins = [t for t in self._confirmed_trades if t.get("pnl") is not None and t["pnl"] > 0]
        self._losses = [t for t in self._confirmed_trades if t.get("pnl") is not None and t["pnl"] < 0]
        return True

    def _confirm_trade(self, result: dict, agent_report: dict) -> Optional[dict]:
        """
        Confirm a trade actually happened.
        
        In paper mode: verify against paper_trades.json ledger.
        In live mode: verify against exchange orders.
        
        Returns the confirmed trade dict, or None if unconfirmed.
        """
        if result.get("mode") == "paper" or self.trading_agent.settings.paper_trading:
            # Paper mode: check the paper trades ledger
            paper_trades = self.trading_agent._load_paper_trades()
            
            # Find the most recent trade matching this result
            for trade in reversed(paper_trades):
                if (trade.get("action") == result.get("action") and
                    trade.get("symbol") == result.get("symbol") and
                    abs(trade.get("price", 0) - result.get("price", 0)) < 0.0001):
                    
                    # Calculate PnL if it's a SELL
                    confirmed = {
                        "timestamp": trade.get("timestamp"),
                        "action": trade.get("action"),
                        "symbol": trade.get("symbol"),
                        "price": trade.get("price"),
                        "quantity": trade.get("quantity"),
                        "mode": "paper",
                        "status": trade.get("status"),
                    }
                    
                    if trade.get("action") == "SELL":
                        entry_price = trade.get("entry_price", 0)
                        if entry_price:
                            confirmed["pnl"] = (trade["price"] - entry_price) * trade.get("quantity", 0)
                            confirmed["return_pct"] = (trade["price"] - entry_price) / entry_price * 100
                    
                    return confirmed
            
            # Could not confirm in paper ledger
            return None
        
        else:
            # Submission is not a fill. QueryOrders is read-only and authoritative.
            order_id = result.get("order_id")
            if not order_id:
                return None
            self._pending_trades[order_id] = dict(result)
            self._pending_trades[order_id].pop("verification_error", None)
            try:
                gateway = self.trading_agent.gateway
                gateway = getattr(gateway, "gateway", gateway)
                order = gateway.query_order(order_id)
                status = order.get("status")
                quantity = float(order["vol_exec"])
                if status in {"closed", "canceled", "expired"} and quantity == 0:
                    self._pending_trades.pop(order_id, None)
                if status not in {"open", "closed", "canceled", "expired"} or not math.isfinite(quantity) or quantity <= 0:
                    return None
            except Exception as exc:
                self._pending_trades[order_id]["verification_error"] = f"Exchange verification unavailable ({type(exc).__name__})"
                return None
            try:
                symbol = gateway.resolve_symbol(order["descr"]["pair"]).wsname
                base, quote = symbol.upper().split("/")
                symbol = f"{'BTC' if base == 'XBT' else base}/{quote}"
                action = order["descr"]["type"].upper()
                price = float(order["price"])
                fee = float(order["fee"]) if order.get("fee") is not None else None
                if action not in {"BUY", "SELL"} or not math.isfinite(price) or price <= 0:
                    return None
                if fee is not None and (not math.isfinite(fee) or fee < 0):
                    return None
            except Exception as exc:
                self._pending_trades[order_id]["verification_error"] = f"Exchange fill metadata unavailable ({type(exc).__name__})"
                return None
            if status in {"closed", "canceled", "expired"}:
                self._pending_trades.pop(order_id, None)
            return {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "order_id": order_id, "status": status, "exchange_status": status,
                "mode": "live", "exchange_verified": True,
                "symbol": symbol, "action": action,
                "quantity": quantity, "fill_quantity": quantity,
                "price": price, "fill_price": price, "fee": fee,
                # An order's proceeds are not realized PnL. Cost basis is unknown.
                "pnl": None, "return_pct": None,
            }

    @staticmethod
    def _rapid_trade_anomalies(previous: Optional[dict], current: dict) -> list[str]:
        """Compare distinct executions, never a fill with its own observation."""
        if not previous:
            return []
        try:
            elapsed = (datetime.fromisoformat(current["timestamp"]) -
                       datetime.fromisoformat(previous["timestamp"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            return []
        return [f"Rapid trading: {elapsed:.1f}s since last trade"] if 0 <= elapsed < 2 else []

    def _check_anomalies(self, agent_report: dict) -> list[str]:
        """Check for suspicious patterns."""
        anomalies = []
        
        # Check if paper PnL is swinging wildly
        if self._confirmed_trades:
            recent_pnls = [
                t.get("pnl", 0) for t in self._confirmed_trades[-5:]
                if t.get("pnl")
            ]
            if len(recent_pnls) >= 3:
                avg = sum(recent_pnls) / len(recent_pnls)
                if abs(avg) > 50 and self.trading_agent.settings.strategy_equity_usd < 1000:
                    anomalies.append(f"Large PnL swings relative to equity: avg {avg:.2f}")
        
        return anomalies
    
    def get_stats(self) -> dict:
        """Performance covers only trades with known realized PnL."""
        known = [t["pnl"] for t in self._confirmed_trades
                 if isinstance(t.get("pnl"), (int, float)) and math.isfinite(t["pnl"])]
        wins = [pnl for pnl in known if pnl > 0]
        losses = [pnl for pnl in known if pnl < 0]
        return {
            "total_confirmed_trades": len(self._confirmed_trades),
            "pending_trades": len(self._pending_trades),
            "pnl_known_trades": len(known),
            "pnl_unknown_trades": len(self._confirmed_trades) - len(known),
            "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(known) if known else None,
            "total_pnl": sum(known) if known else None,
            "average_win": sum(wins) / len(wins) if wins else None,
            "average_loss": sum(losses) / len(losses) if losses else None,
            "paper_trading": self.trading_agent.settings.paper_trading,
        }

    def get_recent_trades(self, n: int = 10) -> list[dict]:
        """Get the N most recent confirmed trades."""
        return self._confirmed_trades[-n:]
    
    def force_check(self) -> dict:
        """Manually trigger a check (for testing or on-demand)."""
        return self.check_and_report()
