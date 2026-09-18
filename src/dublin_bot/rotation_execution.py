"""Strict rotation execution evidence primitives.

These functions do not establish attribution: callers must already have a durable
pre-submit intent (or separately approved, exchange-validated migration record).
They never submit/cancel orders or migrate legacy journals. The advanced engine's
sizing/last_fill fallbacks are intentionally NOT reused as ownership evidence.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math


@dataclass(frozen=True)
class OrderEvidence:
    order_id: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    terminal: bool
    confirmed_at: float
    child_id: str


def read_owned_order(gateway, order_id: str, symbol: str, side: str) -> OrderEvidence:
    """Read exact txid from raw QueryOrders, never a first-result fallback."""
    if not isinstance(order_id, str) or not order_id or "," in order_id:
        raise ValueError("One exact order ID required")
    if side not in {"buy", "sell"}:
        raise ValueError("Invalid expected side")
    result = gateway._private("QueryOrders", {"txid": order_id, "trades": True})
    if not isinstance(result, dict) or not isinstance(result.get(order_id), dict):
        raise ValueError("Missing exact order response")
    raw = result[order_id]
    try:
        meta = gateway.resolve_symbol(symbol)
        # Kraken metadata is the alias authority, not substring/prefix guessing.
        pairs = {meta.key, meta.altname, meta.wsname, symbol}
        if raw["descr"]["pair"] not in pairs or raw["descr"]["type"] != side:
            raise ValueError("Order symbol/side does not match ownership intent")
        qty, requested, cost, fee = (
            Decimal(str(raw[key])) for key in ("vol_exec", "vol", "cost", "fee")
        )
        if any(not value.is_finite() or value < 0 for value in (qty, requested, cost, fee)):
            raise ValueError("Invalid execution numbers")
        if requested <= 0 or qty > requested or (qty > 0 and cost <= 0):
            raise ValueError("Inconsistent executed quantity/cost")
        if not qty and (cost or fee):
            raise ValueError("Unfilled order reports costs")
        status = raw["status"]
        if status not in {"pending", "open", "closed", "canceled", "expired"}:
            raise ValueError("Unknown order status")
        terminal = status in {"closed", "canceled", "expired"}
        # Partial active orders require trade-level timestamps before cooldown
        # accounting; zero deliberately means unavailable, not "just traded".
        confirmed_at = float(raw["closetm"]) if terminal and qty else 0.0
        if not math.isfinite(confirmed_at) or (terminal and qty and confirmed_at <= 0):
            raise ValueError("Invalid confirmation timestamp")
        child = raw.get("closetxid") or ""
        if not isinstance(child, str):
            raise ValueError("Malformed protective child ID")
        return OrderEvidence(order_id, qty, cost / qty if qty else Decimal(0),
                             fee, terminal, confirmed_at, child)
    except (KeyError, TypeError, InvalidOperation, AttributeError) as exc:
        raise ValueError("Incomplete exchange order evidence") from exc



@dataclass(frozen=True)
class AccountEvidence:
    """Necessary, not sufficient, input to risk; unknown realized PnL blocks BUY."""
    equity: Decimal
    cash_available: Decimal
    open_orders: dict
    realized_pnl_today: None = None


def read_account_evidence(gateway) -> AccountEvidence:
    """Strict private reads; no equity/balance/order wrapper with silent fallbacks.

    BalanceEx separates reserved spot funds from cash. Credit is never counted
    as spendable capital. This is not an atomic exchange snapshot, and provides
    no ownership, cost-basis, fee estimate or realized-PnL assurance.
    """
    try:
        equity = Decimal(str(gateway._private("TradeBalance", {"asset": "ZUSD"})["eb"]))
        if not equity.is_finite() or equity < 0:
            raise ValueError("Invalid authoritative equity")
        balances = gateway._private("BalanceEx", {})
        usd = balances["ZUSD"]
        cash, held = Decimal(str(usd["balance"])), Decimal(str(usd["hold_trade"]))
        if any(not n.is_finite() or n < 0 for n in (cash, held)) or held > cash:
            raise ValueError("Invalid authoritative available cash")
        orders = gateway._private("OpenOrders", {"trades": True})["open"]
        if not isinstance(orders, dict):
            raise ValueError("Invalid authoritative open-order book")
        return AccountEvidence(equity, cash - held, orders)
    except (KeyError, TypeError, InvalidOperation, AttributeError) as exc:
        raise ValueError("Incomplete authoritative account evidence; reconciliation required") from exc

# Execution state is one atomically committed document: intents, fills, cost
# basis, strategy projection, risk high water and cooldown cannot tear apart.
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from .risk import RiskManager, SessionState
from .precision import size_order


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite exchange/state value")
    return result


def complete_history(gateway, endpoint, key):
    """Fail on changing totals, duplicate pages or truncated history."""
    rows = {}; total = None
    while True:
        page = gateway._private(endpoint, {"ofs": len(rows)})
        count = page["count"]
        if not isinstance(count, int) or count < 0 or (total is not None and count != total):
            raise ValueError("Unstable history count")
        total = count; batch = page[key]
        if not isinstance(batch, dict) or set(batch) & set(rows):
            raise ValueError("Duplicate/malformed history page")
        rows.update(batch)
        if len(rows) == total:
            return rows
        if not batch or len(rows) > total:
            raise ValueError("Incomplete exchange history")


class RotationExecutor:
    """Spot-only rotation. No legacy engine execution or account-wide sale.

    The journal must be unique for this coordinator/account. A missing journal
    bootstraps only an exchange-proven virgin, cash-only account; otherwise an
    explicit historical migration is required. Ambiguous writes never resend.
    """
    def __init__(self, gateway, settings, path=None, risk_manager=None):
        self.gateway = gateway; self.settings = settings
        self.path = Path(path or Path(settings.session_state_path).with_name("rotation_live_state.json"))
        self.risk = risk_manager or RiskManager(settings)

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path)+".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.state = self.snapshot()
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)

    def snapshot(self):
        if not self.path.exists():
            return {"version":1, "initialized":False, "orders":{}, "lots":{},
                    "fills":{}, "realized":"0", "last_exit":{}, "last_fill":0,
                    "blocked":None}
        data = json.loads(self.path.read_text())
        try:
            if data["version"] != 1 or any(not isinstance(data[k],dict) for k in ("orders","lots","fills","last_exit")):
                raise ValueError("Invalid schema")
            expected={}
            for fill in data["fills"].values():
                qty=decimal(fill["quantity"])
                if qty<=0 or fill["side"] not in {"buy","sell"}:
                    raise ValueError("Invalid fill")
                symbol=fill["symbol"]
                expected[symbol]=expected.get(symbol,Decimal(0))+(qty if fill["side"]=="buy" else -qty)
            for symbol in set(expected)|set(data["lots"]):
                lot=data["lots"][symbol]
                qty=decimal(lot["qty"])
                if qty<0 or qty!=expected.get(symbol,Decimal(0)) or decimal(lot["basis"])<0:
                    raise ValueError("Lot/fill discrepancy")
                if qty and decimal(lot["entry_price"])<=0:
                    raise ValueError("Invalid cost basis")
            if decimal(data["realized"])!=sum((decimal(f["pnl"]) for f in data["fills"].values()),Decimal(0)):
                raise ValueError("Realized ledger discrepancy")
            last=max((f["time"] for f in data["fills"].values()),default=0)
            if last!=data["last_fill"] or not math.isfinite(last):
                raise ValueError("Cooldown/fill discrepancy")
            if data["initialized"] and (decimal(data["start_equity"])<=0 or decimal(data["peak_equity"])<decimal(data["start_equity"])):
                raise ValueError("Invalid risk high-water")
        except (KeyError,TypeError,ValueError,InvalidOperation) as exc:
            raise ValueError("Invalid live execution journal; reconciliation required") from exc
        return data

    def _save(self):
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".rotation-live-")
        try:
            with os.fdopen(fd,"w") as out:
                json.dump(self.state,out,allow_nan=False); out.flush(); os.fsync(out.fileno())
            os.replace(name,self.path)
            directory = os.open(self.path.parent,os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(name): os.unlink(name)

    def holdings(self):
        return {s:float(decimal(l["qty"])) for s,l in self.snapshot()["lots"].items()
                if decimal(l["qty"]) > 0}

    def _initialize(self, account):
        if self.state["initialized"]: return
        history = complete_history(self.gateway,"TradesHistory","trades")
        ledger = complete_history(self.gateway,"Ledgers","ledger")
        balances = self.gateway._private("BalanceEx",{})
        if history or account.open_orders or any(
            k != "ZUSD" and decimal(v["balance"]) != 0 for k,v in balances.items()
        ) or any(v["asset"] != "ZUSD" or v["type"] not in {"deposit","withdrawal"} for v in ledger.values()):
            raise ValueError("Historical ownership/risk migration required; no account-balance adoption")
        funding=sum((decimal(v["amount"])-decimal(v["fee"]) for v in ledger.values()),Decimal(0))
        if funding<=0 or funding!=decimal(balances["ZUSD"]["balance"]) or funding!=account.equity:
            raise ValueError("Complete funding ledger/equity evidence required for bootstrap")
        self.state.update(initialized=True,start_equity=str(account.equity),peak_equity=str(account.equity))
        self._save()

    def _submit(self, symbol, side, params):
        self.gateway._assert_can_submit()
        token = uuid.uuid4().hex
        # Reference persists before dispatch; lack of an exact response ID is
        # permanently pending until explicit evidence-based recovery.
        params = dict(params, userref=str(int(token[:7],16)), oflags="fciq")
        record = dict(symbol=symbol,side=side,params=params,txid=None,terminal=False,
                      created=time.time(),children=[],intent=token)
        self.state["orders"][token]=record
        self._save()
        response=self.gateway._private("AddOrder",params)
        ids=response.get("txid")
        if not isinstance(ids,list) or len(ids)!=1 or not isinstance(ids[0],str) or not ids[0]:
            raise ValueError("Ambiguous AddOrder response; intent retained")
        record["txid"]=ids[0]; self._save()
        return record

    def _read(self, record):
        oid=record["txid"]
        if not oid: raise ValueError("Pending ambiguous submission; recovery required (never resubmit)")
        raw=self.gateway._private("QueryOrders",{"txid":oid,"trades":True})
        # Reuse the strict exact-ID validator without a second exchange read.
        class Snapshot:
            def _private(_, *args): return raw
            def resolve_symbol(_, symbol): return self.gateway.resolve_symbol(symbol)
        ev=read_owned_order(Snapshot(),oid,record["symbol"],record["side"])
        order=raw[oid]
        if decimal(order["vol"]) != decimal(record["params"]["volume"]):
            raise ValueError("Order requested volume differs from durable intent")
        return ev,order

    def _reconcile_order(self, record):
        ev, raw = self._read(record)
        ids=raw.get("trades",[])
        if not isinstance(ids,list) or len(set(ids))!=len(ids) or (ev.quantity and not ids):
            raise ValueError("Missing/duplicate authoritative trade IDs")
        previous={tid:f for tid,f in self.state["fills"].items() if f["order_id"]==ev.order_id}
        if not set(previous).issubset(ids) or (record.get("terminal") and not ev.terminal):
            raise ValueError("Exchange order/trade history regression")
        trades=self.gateway._private("QueryTrades",{"txid":",".join(ids)}) if ids else {}
        if set(trades)!=set(ids): raise ValueError("Incomplete exact trade records")
        meta=self.gateway.resolve_symbol(record["symbol"])
        sums=[Decimal(0),Decimal(0),Decimal(0)]
        ledger=complete_history(self.gateway,"Ledgers","ledger") if ids else {}
        validated=[]
        for tid,t in sorted(trades.items(),key=lambda item:float(item[1]["time"])):
            if t["ordertxid"]!=ev.order_id or t["type"]!=record["side"] or t["pair"] not in {meta.key,meta.altname,meta.wsname,record["symbol"]}:
                raise ValueError("Trade attribution mismatch")
            qty,cost,fee=map(decimal,(t["vol"],t["cost"],t["fee"]))
            stamp=float(t["time"])
            if min(qty,cost)<=0 or fee<0 or not math.isfinite(stamp) or stamp<=0:
                raise ValueError("Invalid confirmed fill")
            entries=[v for v in ledger.values() if v.get("refid")==tid and v.get("type")=="trade"]
            base=[v for v in entries if v["asset"]==meta.base]
            quote=[v for v in entries if v["asset"]==meta.quote]
            if len(base)!=1 or len(quote)!=1 or len(entries)!=2:
                raise ValueError("Fee currency/ledger evidence incomplete")
            b,q=base[0],quote[0]; sign=1 if record["side"]=="buy" else -1
            if decimal(b["amount"])!=sign*qty or decimal(q["amount"])!=-sign*cost:
                raise ValueError("Trade/ledger amount mismatch")
            bf,qf=decimal(b["fee"]),decimal(q["fee"])
            # Quote-fee request must be corroborated by the ledger. Base fees
            # require separate valuation; never overstate sellable ownership.
            if bf != 0 or qf != fee:
                raise ValueError("Non-quote fee requires explicit reconciliation")
            sums=[a+b for a,b in zip(sums,(qty,cost,fee))]
            if tid in self.state["fills"]:
                prior=self.state["fills"][tid]
                if (prior["order_id"],prior["symbol"],prior["side"],decimal(prior["quantity"]),
                    decimal(prior["cost"]),decimal(prior["fee"]),prior["time"]) != (
                    ev.order_id,record["symbol"],record["side"],qty,cost,fee,stamp):
                    raise ValueError("Previously confirmed fill changed")
            validated.append((tid,qty,cost,fee,stamp))
        if sums != [ev.quantity,decimal(raw["cost"]),ev.fee]:
            raise ValueError("Cumulative order/trade totals disagree")
        symbol=record["symbol"]
        for tid,qty,cost,fee,stamp in validated:
            if tid in self.state["fills"]: continue
            lot=self.state["lots"].setdefault(symbol,{"qty":"0","basis":"0","entry_price":"0"})
            old=decimal(lot["qty"]); basis=decimal(lot["basis"])
            pnl=Decimal(0)
            if record["side"]=="buy":
                lot.update(qty=str(old+qty),basis=str(basis+cost+fee),entry_price=str((basis+cost)/(old+qty)))
            else:
                if qty>old: raise ValueError("Exit exceeds exact owned lot")
                allocated=basis*qty/old
                pnl=cost-fee-allocated
                lot.update(qty=str(old-qty),basis=str(basis-allocated))
                self.state["realized"]=str(decimal(self.state["realized"])+pnl)
                self.state["last_exit"][symbol]=max(stamp,self.state["last_exit"].get(symbol,0))
            self.state["fills"][tid]=dict(order_id=ev.order_id,symbol=symbol,side=record["side"],
                quantity=str(qty),cost=str(cost),fee=str(fee),time=stamp,pnl=str(pnl))
            self.state["last_fill"]=max(stamp,self.state["last_fill"])
        record["terminal"]=ev.terminal
        if record["side"]=="buy" and ev.child_id and ev.child_id not in record["children"]:
            record["children"].append(ev.child_id)
        self._save()
        return ev,raw

    def _children(self, parent):
        # All pages, exact exchange parent linkage, never colliding userrefs.
        book = self.gateway._private("OpenOrders", {"trades": True})["open"]
        closed = complete_history(self.gateway, "ClosedOrders", "closed")
        for oid, raw in {**closed, **book}.items():
            if raw.get("refid") == parent["txid"] and oid not in parent["children"]:
                parent["children"].append(oid)
        self._save()
        children=[]
        for oid in parent["children"]:
            raw=self.gateway._private("QueryOrders",{"txid":oid,"trades":True})[oid]
            if raw["descr"]["ordertype"]!="stop-loss":
                raise ValueError("Unexpected protective child type")
            # Exact child ID came from exact parent response, never userref search.
            child=dict(txid=oid,symbol=parent["symbol"],side="sell",children=[],
                       params={"volume":raw["vol"]},terminal=False)
            children.append(child)
        parent_ev, _ = self._read(parent)
        if sum((decimal(c["params"]["volume"]) for c in children), Decimal(0)) != parent_ev.quantity:
            raise ValueError("Protective child coverage incomplete or over-sized")
        return children

    def _reconcile(self):
        for r in list(self.state["orders"].values()):
            self._reconcile_order(r)
            if r["side"]=="buy":
                for c in self._children(r): self._reconcile_order(c)
        # A later poll can finish a partial exit just as sell() can. Clear its
        # durable resume flag in the same reconciliation transaction, otherwise
        # a subsequent re-entry would inherit the old request to liquidate.
        for symbol in list(self.state.get("exit_requested", {})):
            if (decimal(self.state["lots"].get(symbol, {}).get("qty", 0)) == 0
                    and all(r["terminal"] for r in self.state["orders"].values()
                            if r["symbol"] == symbol)):
                self.state["exit_requested"].pop(symbol)
        self._save()

    def reconcile(self):
        with self._transaction():
            try:
                self._reconcile(); self.state["blocked"]=None; self._save()
                return self.snapshot()
            except Exception as exc:
                self.state=self.snapshot(); self.state["blocked"]=str(exc); self._save()
                raise

    def recover_pending(self):
        """Read-only exchange recovery. No absence-based clearing or resubmission."""
        def action():
            pending=[r for r in self.state["orders"].values() if not r["txid"]]
            if pending:
                opened=self.gateway._private("OpenOrders",{"trades":True})["open"]
                closed=complete_history(self.gateway,"ClosedOrders","closed")
                if set(opened)&set(closed):
                    raise ValueError("Changing order books; recovery ambiguous")
                book={**opened,**closed}
                for r in pending:
                    matches=[(oid,o) for oid,o in book.items()
                             if str(o.get("userref"))==r["params"]["userref"]]
                    if len(matches)!=1:
                        raise ValueError("Pending recovery ambiguous: reference absent or colliding")
                    oid,raw=matches[0]
                    meta=self.gateway.resolve_symbol(r["symbol"])
                    descr=raw["descr"];stamp=float(raw["opentm"])
                    if (descr["pair"] not in {meta.key,meta.altname,meta.wsname,r["symbol"]}
                        or descr["type"]!=r["side"] or descr["ordertype"]!=r["params"]["ordertype"]
                        or decimal(raw["vol"])!=decimal(r["params"]["volume"])
                        or not math.isfinite(stamp) or stamp<r["created"]-5 or stamp>r["created"]+300):
                        raise ValueError("Pending recovery intent mismatch")
                    if "price" in r["params"] and decimal(descr["price"])!=decimal(r["params"]["price"]):
                        raise ValueError("Pending recovery price mismatch")
                    if any(other["txid"]==oid for other in self.state["orders"].values()):
                        raise ValueError("Pending recovery order already attributed")
                    r["txid"]=oid;self._save()
            self._reconcile()
        return self._run(action)

    def _run(self, action):
        with self._transaction():
            before=set(self.state["fills"])
            try:
                action(); self.state["blocked"]=None; self._save()
                return self._result(before)
            except Exception as exc:
                # Reload last durable transaction, not partially mutated memory.
                self.state=self.snapshot(); self.state["blocked"]=str(exc); self._save()
                return dict(self._result(before), error=str(exc), pending=True)

    def _result(self, previous_ids):
        fills=[dict(fill,trade_id=tid) for tid,fill in self.state["fills"].items() if tid not in previous_ids]
        result={"executed":bool(fills), "confirmed_fills":fills,
                "pending":any(not r["terminal"] for r in self.state["orders"].values())}
        if fills:
            latest=max(fills,key=lambda f:f["time"])
            result.update(order_id=latest["order_id"], action=latest["side"].upper(),
                          symbol=latest["symbol"], mode="live")
        return result

    def buy(self,symbol,signal):
        def action():
            if symbol not in (self.settings.universe_allowlist or self.settings.coin_basket):
                raise ValueError("Symbol outside configured universe")
            self._reconcile()
            if any(not r["terminal"] for r in self.state["orders"].values()) or self.holdings():
                raise ValueError("Existing owned lot or pending order blocks new entry")
            if time.time()-self.state["last_exit"].get(symbol,0)<180:
                raise ValueError("Per-symbol 3-minute confirmed-exit cooldown")
            account=read_account_evidence(self.gateway); self._initialize(account)
            history=complete_history(self.gateway,"TradesHistory","trades")
            if set(history)!=set(self.state["fills"]):
                raise ValueError("Unattributed exchange history; realized PnL unavailable")
            if account.open_orders: raise ValueError("Unreconciled account open exposure")
            today=datetime.now(timezone.utc).date()
            fills=[f for f in self.state["fills"].values() if datetime.fromtimestamp(f["time"],timezone.utc).date()==today]
            self.state["peak_equity"]=str(max(account.equity,decimal(self.state["peak_equity"])))
            self._save()
            last=self.state["last_fill"]
            session=SessionState(float(self.state["start_equity"]),float(self.state["peak_equity"]),float(account.equity),
                float(sum((decimal(f["pnl"]) for f in fills),Decimal(0))),
                len({f["order_id"] for f in fills}),datetime.fromtimestamp(last,timezone.utc) if last else None)
            decision=self.risk.evaluate(signal,session,open_exposure_usd=float(max(Decimal(0),account.equity-account.cash_available)))
            if not decision.approved: raise ValueError(decision.reason)
            meta=self.gateway.resolve_symbol(symbol)
            if not meta.tradable or meta.quote not in {"USD","ZUSD"}: raise ValueError("Not a tradable USD spot pair")
            fees=self.gateway.fee_schedule(meta.key)
            if not fees: raise ValueError("Authoritative fee schedule unavailable")
            fee=decimal(fees["taker_bps"])/10000
            if fee<0: raise ValueError("Invalid fee schedule")
            notional=min(decimal(decision.notional_usd),account.cash_available/(1+fee))
            ticker=self.gateway.get_ticker_for(symbol)
            price=decimal(ticker["ask"])
            sized=size_order(float(notional),float(price),meta.to_precision(),min_notional_usd=self.settings.min_order_notional_usd)
            stop=decimal(signal.stop_price).quantize(Decimal(1).scaleb(-meta.pair_decimals))
            if stop<=0 or stop>=price: raise ValueError("Invalid rounded protective stop")
            params=dict(pair=meta.key,type="buy",ordertype="market",volume=sized.volume_str)
            if self.settings.order_type=="limit":
                from decimal import ROUND_DOWN
                entry=(price*(1-decimal(self.settings.limit_offset_pct))).quantize(Decimal(1).scaleb(-meta.pair_decimals),rounding=ROUND_DOWN)
                if entry>=price: entry-=Decimal(1).scaleb(-meta.pair_decimals)
                if entry<=stop: raise ValueError("Limit entry must exceed stop")
                params.update(ordertype="limit",price=str(entry))
            params.update({"close[ordertype]":"stop-loss","close[price]":str(stop)})
            record=self._submit(symbol,"buy",params); self._reconcile_order(record)
        return self._run(action)

    def preview_migration(self, local_journal, risk_state):
        """Read-only candidate, NEVER a runtime migration or protection claim.

        Explicit local journal IDs prove requested attribution; complete exchange
        history, exact orders/trades and the full asset ledger corroborate it.
        Unknown external trade basis and unsupported non-trade asset movements
        reject the preview. Persisted risk high-water must be supplied, not reset.
        Applying the returned state is a separate authorized deployment step.
        """
        import re
        if self.path.exists():
            raise ValueError("Existing live journal requires reconciliation, not migration")
        if not isinstance(local_journal, list) or not local_journal:
            raise ValueError("Explicit local journal order IDs required")
        ids=[r["order_id"] for r in local_journal]
        if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[A-Z0-9]{6}-[A-Z0-9]{5}-[A-Z0-9]{6}",x) for x in ids):
            raise ValueError("Invalid or duplicate literal journal IDs")
        account=read_account_evidence(self.gateway)
        start,peak=decimal(risk_state["start_equity"]),decimal(risk_state["peak_equity"])
        if start<=0 or peak<start:
            raise ValueError("Persisted risk high-water evidence required")
        trades=complete_history(self.gateway,"TradesHistory","trades")
        ledger=complete_history(self.gateway,"Ledgers","ledger")
        closed=complete_history(self.gateway,"ClosedOrders","closed")
        book={**closed,**account.open_orders}
        records={r["order_id"]:dict(r) for r in local_journal}
        # Children require exchange parent linkage or exact parent closetxid.
        for oid,r in list(records.items()):
            raw=self.gateway._private("QueryOrders",{"txid":oid,"trades":True})[oid]
            for cid,c in book.items():
                if c.get("refid")==oid or cid==raw.get("closetxid"):
                    records[cid]={"order_id":cid,"symbol":r["symbol"],"side":"sell"}
        if any(t["ordertxid"] not in records for t in trades.values()):
            raise ValueError("Unattributed history/cost basis prevents migration")
        totals={}
        for row in ledger.values():
            asset=row["asset"]
            if row["type"] not in {"trade","deposit","withdrawal"}:
                raise ValueError("Unsupported ledger event in historical migration")
            if row["type"]!="trade" and asset!="ZUSD":
                raise ValueError("External base-asset movement makes ownership ambiguous")
            totals[asset]=totals.get(asset,Decimal(0))+decimal(row["amount"])-decimal(row["fee"])
        balances=self.gateway._private("BalanceEx",{})
        for asset in set(totals)|set(balances):
            if totals.get(asset,Decimal(0))!=decimal(balances.get(asset,{"balance":"0"})["balance"]):
                raise ValueError("Historical ledger/current balance mismatch")
        preview=copy.copy(self)
        preview.state=self.snapshot()
        preview.state.update(initialized=True,start_equity=str(start),peak_equity=str(max(peak,account.equity)))
        preview._save=lambda: None
        # Replay exact orders in first-fill order; overlapping legacy inventory
        # that cannot be replayed safely fails rather than guessing allocation.
        def first_fill(item):
            return min((float(t["time"]) for t in trades.values() if t["ordertxid"]==item[0]),default=float("inf"))
        for oid,record in sorted(records.items(),key=first_fill):
            raw=self.gateway._private("QueryOrders",{"txid":oid,"trades":True})[oid]
            r=dict(txid=oid,symbol=record["symbol"],side=record["side"],
                   params={"volume":raw["vol"]},children=[],terminal=False,intent="migration:"+oid)
            preview.state["orders"][r["intent"]]=r
            preview._reconcile_order(r)
        if set(preview.state["fills"]) != set(trades):
            raise ValueError("Trade history and order records disagree")
        if any(oid not in records for oid in account.open_orders):
            raise ValueError("Unattributed open orders prevent migration")
        unprotected=[]
        for symbol,lot in preview.state["lots"].items():
            qty=decimal(lot["qty"])
            if qty<=0: continue
            meta=self.gateway.resolve_symbol(symbol)
            if qty!=decimal(balances[meta.base]["balance"]):
                raise ValueError("Owned historical lot/current balance mismatch")
            protected=sum((decimal(o["vol"])-decimal(o["vol_exec"]) for oid,o in account.open_orders.items()
                           if records[oid]["symbol"]==symbol and o["descr"]["type"]=="sell"
                           and o["descr"]["ordertype"]=="stop-loss"),Decimal(0))
            if protected!=qty: unprotected.append(symbol)
        return {"dry_run":True,"state":preview.state,"protection_required":unprotected,
                "local_journal_ids":ids,"trade_count":len(trades),"ledger_count":len(ledger),
                "deployment_ready":False}

    def _cancel_final(self, record):
        self.gateway._assert_can_submit()
        oid=record["txid"]
        cancellations=self.state.setdefault("cancellations",{})
        if oid not in cancellations:
            cancellations[oid]={"requested":time.time(),"terminal":False}
            self._save()
            try:
                self.gateway._private("CancelOrder",{"txid":oid})
            except Exception as exc:
                cancellations[oid]["response_error"]=str(exc)
                self._save()
        # Cancellation acknowledgements/disappearance are never fill evidence.
        # On response loss, exact final QueryOrders can still prove completion.
        evidence,_=self._reconcile_order(record)
        if not evidence.terminal:
            raise ValueError("Cancellation pending/ambiguous; no retry or market sale")
        cancellations[oid]["terminal"]=True
        self._save()
        return evidence

    def sell(self,symbol):
        def action():
            if symbol not in self.state["lots"]:
                raise ValueError("No durable lot ownership for requested symbol")
            self._reconcile()
            meta=self.gateway.resolve_symbol(symbol)
            history=complete_history(self.gateway,"TradesHistory","trades")
            aliases={meta.key,meta.altname,meta.wsname,symbol}
            if any(tid not in self.state["fills"] and t["pair"] in aliases
                   for tid,t in history.items()):
                raise ValueError("Unattributed same-asset trade requires ownership reconciliation")
            ledger=complete_history(self.gateway,"Ledgers","ledger")
            if any(row["asset"]==meta.base and row["type"]!="trade" and decimal(row["amount"])<0
                   for row in ledger.values()):
                raise ValueError("Unattributed base-asset outflow requires ownership reconciliation")
            self.state.setdefault("exit_requested",{})[symbol]=True
            self._save()  # durable exit intent before cancelling any protection
            for r in self.state["orders"].values():
                if r["symbol"]!=symbol: continue
                if not r["terminal"]:
                    if r["side"]=="sell": raise ValueError("Pending exit blocks duplicate sale")
                    self._cancel_final(r)
                if r["side"]=="buy":
                    ev,_=self._reconcile_order(r)
                    if ev.quantity and not r["children"]: raise ValueError("Protective child identity unresolved")
                    for c in self._children(r):
                        child,_=self._reconcile_order(c)
                        if not child.terminal:
                            self._cancel_final(c)
            qty=decimal(self.state["lots"].get(symbol,{}).get("qty",0))
            if not qty:
                self.state["exit_requested"].pop(symbol,None)
                self._save()
                return
            meta=self.gateway.resolve_symbol(symbol)
            balance=self.gateway._private("BalanceEx",{})[meta.base]
            if decimal(balance["balance"])-decimal(balance["hold_trade"])<qty:
                raise ValueError("Owned quantity exceeds authoritative available balance")
            if qty.quantize(Decimal(1).scaleb(-meta.lot_decimals))!=qty or qty<meta.order_min:
                raise ValueError("Exact owned quantity not sellable; dust recovery required")
            r=self._submit(symbol,"sell",dict(pair=meta.key,type="sell",ordertype="market",volume=str(qty)))
            self._reconcile_order(r)
            if decimal(self.state["lots"][symbol]["qty"])==0:
                self.state["exit_requested"].pop(symbol,None)
                self._save()
        return self._run(action)
