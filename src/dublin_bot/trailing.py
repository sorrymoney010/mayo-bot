"""ATR trailing take-profit for paper positions.

Once unrealized gain reaches ``activate_atr`` times the entry ATR, a stop is
armed at ``trail_atr`` times ATR behind the best price seen. The stop only
ratchets in the trade's favor. A later price through that stop exits.

State is a JSON file keyed by ``sleeve|symbol|opened_at`` so a restart keeps
an already-armed trail on an open position. This module never submits an
order; callers book the paper exit themselves.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_PATH = Path("logs/trailing_state.json")


@dataclass
class TrailState:
    sleeve: str
    symbol: str
    side: str                 # "long" | "short"
    entry: float
    atr: float
    activate_atr: float
    trail_atr: float
    opened_at: str = ""
    peak: float = 0.0         # best price: highest for a long, lowest for a short
    armed: bool = False
    stop: float | None = None

    def key(self) -> str:
        return f"{self.sleeve}|{self.symbol}|{self.opened_at}"


def _favorable(side: str, price: float, peak: float) -> bool:
    if side == "short":
        return price < peak
    return price > peak


def _gain(side: str, entry: float, peak: float) -> float:
    if side == "short":
        return entry - peak
    return peak - entry


def _stop_from_peak(side: str, peak: float, atr: float, trail_atr: float) -> float:
    dist = trail_atr * atr
    if side == "short":
        return peak + dist
    return peak - dist


def _breached(side: str, price: float, stop: float) -> bool:
    if side == "short":
        return price >= stop
    return price <= stop


def fresh_state(*, sleeve: str, symbol: str, side: str, entry: float, atr: float,
                activate_atr: float, trail_atr: float, opened_at: str = "",
                last_price: float | None = None) -> TrailState:
    """New or restored-from-entry state. A known last price seeds the peak."""
    side = "short" if side == "short" else "long"
    peak = float(entry)
    if last_price is not None and last_price > 0:
        if side == "short":
            peak = min(peak, float(last_price))
        else:
            peak = max(peak, float(last_price))
    st = TrailState(
        sleeve=sleeve, symbol=symbol, side=side, entry=float(entry), atr=float(atr),
        activate_atr=float(activate_atr), trail_atr=float(trail_atr),
        opened_at=str(opened_at or ""), peak=peak,
    )
    return _maybe_arm(st)


def _maybe_arm(st: TrailState) -> TrailState:
    if st.atr <= 0 or st.activate_atr <= 0 or st.trail_atr <= 0:
        return st
    if _gain(st.side, st.entry, st.peak) + 1e-12 >= st.activate_atr * st.atr:
        st.armed = True
        new_stop = _stop_from_peak(st.side, st.peak, st.atr, st.trail_atr)
        if st.stop is None:
            st.stop = new_stop
        elif st.side == "short":
            st.stop = min(st.stop, new_stop)
        else:
            st.stop = max(st.stop, new_stop)
    return st


def on_price(st: TrailState, price: float) -> tuple[TrailState, str | None]:
    """Apply one trade price.

    An already-armed stop is tested *before* the peak ratchets, so the tick
    that arms the trail cannot also stop out on that same print.
    """
    if price <= 0:
        return st, None
    if st.armed and st.stop is not None and _breached(st.side, price, st.stop):
        return st, "trail"
    if st.peak <= 0:
        st.peak = price
    elif _favorable(st.side, price, st.peak):
        st.peak = price
    was_armed = st.armed
    st = _maybe_arm(st)
    if st.armed and not was_armed:
        return st, None
    return st, None


def on_bar(st: TrailState, *, open_: float, high: float, low: float
           ) -> tuple[TrailState, str | None, float | None]:
    """Bar path used by the walk-forward. Stop is checked before the peak updates.

    A gap through the stop fills at the open. Stop and trail are taker exits.
    The arming bar does not stop out on its own wick.
    """
    if st.armed and st.stop is not None:
        if st.side == "short":
            if open_ >= st.stop:
                return st, "trail_gap", float(open_)
            if high >= st.stop:
                return st, "trail", float(st.stop)
        else:
            if open_ <= st.stop:
                return st, "trail_gap", float(open_)
            if low <= st.stop:
                return st, "trail", float(st.stop)
    extreme = low if st.side == "short" else high
    if extreme > 0:
        st, _ = on_price(st, extreme)
    return st, None, None


class TrailBook:
    """Persisted trails. Atomic replace so a crash keeps the previous file."""

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path)
        self.states: dict[str, TrailState] = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        self.states = {}
        if not isinstance(raw, dict):
            return
        for _key, row in raw.items():
            if not isinstance(row, dict):
                continue
            try:
                st = TrailState(
                    sleeve=str(row["sleeve"]), symbol=str(row["symbol"]), side=str(row["side"]),
                    entry=float(row["entry"]), atr=float(row["atr"]),
                    activate_atr=float(row["activate_atr"]), trail_atr=float(row["trail_atr"]),
                    opened_at=str(row.get("opened_at") or ""), peak=float(row.get("peak") or row["entry"]),
                    armed=bool(row.get("armed")), stop=None if row.get("stop") is None else float(row["stop"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            self.states[st.key()] = st

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in self.states.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, st: TrailState) -> TrailState:
        return self.states.get(st.key(), st)

    def put(self, st: TrailState) -> None:
        self.states[st.key()] = st

    def drop(self, st: TrailState) -> None:
        self.states.pop(st.key(), None)

    def reconcile(self, open_keys: set[str]) -> None:
        """Drop trails whose position is gone. Keep the rest verbatim."""
        self.states = {k: v for k, v in self.states.items() if k in open_keys}


def maintain(settings, sleeve: str, symbol: str, side: str, entry: float, price: float,
            opened_at: str, atr: float) -> str | None:
    """Update the persisted trail. Returns 'trail' when the price crosses it.

    Stop and take-profit checks stay with the caller and win when both hit:
    call this only after those levels have missed.
    """
    if not sleeve_trail_enabled(settings, sleeve):
        return None
    if price <= 0 or entry <= 0:
        return None
    book = TrailBook(getattr(settings, "trailing_state_path", DEFAULT_PATH))
    seed = fresh_state(
        sleeve=sleeve, symbol=symbol, side=side, entry=entry,
        atr=float(atr or 0.0),
        activate_atr=float(getattr(settings, "trailing_activate_atr", 1.5)),
        trail_atr=float(getattr(settings, "trailing_atr_mult", 1.0)),
        opened_at=opened_at, last_price=price,
    )
    st = book.get(seed)
    if st.atr <= 0 < float(atr or 0.0):
        st.atr = float(atr)
        st = _maybe_arm(st)
    if st.atr <= 0:
        return None
    st, reason = on_price(st, price)
    if reason:
        book.drop(st)
    else:
        book.put(st)
    book.save()
    return reason


def sleeve_trail_enabled(settings, sleeve: str) -> bool:
    """Per-sleeve default. A sleeve is on only after its walk-forward improves."""
    flag = {
        "primary": "trailing_tp_regime",
        "regime": "trailing_tp_regime",
        "regime_trend": "trailing_tp_regime",
        "meanrev_4h": "trailing_tp_meanrev",
        "meanrev": "trailing_tp_meanrev",
        "trendhold_4h": "trailing_tp_trendhold",
        "trendhold": "trailing_tp_trendhold",
        "futures_short": "trailing_tp_futures",
    }.get(sleeve, "")
    return bool(flag and getattr(settings, flag, False))
