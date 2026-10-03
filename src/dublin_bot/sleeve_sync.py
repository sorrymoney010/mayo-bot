"""One commit path for sleeve state and the shared paper lot file.

The fast exit watcher and the 300s sleeves both write
``meanrev_sleeve.json``, ``trendhold_sleeve.json`` and
``paper_bot_positions.json``. A sleeve loads those files at the start of a
cycle and used to save its in-memory copy at the end, which put a lot back
after the watcher had already closed it. Cash stayed right; ownership and
position counts did not.

``commit_sleeve_cycle`` holds the paper-book lock, reloads the book, and
refuses to write a position the book no longer holds.
"""
from __future__ import annotations

import json
from pathlib import Path

from .paper import paper_book_lock


def _open_qty(portfolio, equity: float) -> dict[str, float]:
    snap = portfolio.load(equity=equity, cash=equity)
    return {
        sym: float(pos.quantity)
        for sym, pos in snap.positions.items()
        if float(pos.quantity) > 1e-12
    }


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def commit_sleeve_cycle(portfolio, state: dict, *, state_path: Path | str,
                        lots_path: Path | str, save_state, equity: float) -> dict[str, float]:
    """Drop closed symbols from ``state`` and rewrite lots from the book.

    ``save_state`` persists the sleeve file. It must not take the paper-book
    lock itself; this function already holds it. Returns the lot map written.
    """
    lots_path = Path(lots_path)
    with paper_book_lock(portfolio.path):
        # Reload even if this thread already holds a stale snapshot.
        open_qty = _open_qty(portfolio, equity)
        positions = state.get("positions")
        if isinstance(positions, dict):
            for sym in list(positions):
                if sym not in open_qty:
                    positions.pop(sym, None)
        save_state(state)
        _write_json(lots_path, open_qty)
        return dict(open_qty)


def save_paper_bot_qty(path: Path | str, bot_qty: dict, portfolio, *, equity: float,
                       in_flight: set[str] | None = None) -> None:
    """Write the primary lot file without resurrecting a watcher close.

    A symbol still in memory but gone from the book is dropped, unless this
    call is the in-flight buy that has not reached the book yet (``in_flight``).
    Quantities for symbols the book still holds are taken from the book.
    """
    path = Path(path)
    pending = in_flight or set()
    with paper_book_lock(portfolio.path):
        book = _open_qty(portfolio, equity)
        for sym in list(bot_qty):
            if sym in book:
                bot_qty[sym] = book[sym]
            elif sym not in pending:
                bot_qty.pop(sym, None)
        # Keep lots the book still holds that this process loaded and did not
        # sell. An in-flight sell has already removed the symbol from bot_qty;
        # do not copy it back from the book (the paper fill follows this write).
        out = {sym: qty for sym, qty in book.items() if sym in bot_qty}
        for sym, qty in bot_qty.items():
            out.setdefault(sym, qty)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out), encoding="utf-8")
