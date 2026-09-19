import base64
from decimal import Decimal
from unittest.mock import Mock

import pytest

import dublin_bot.rotation_execution as execution
from dublin_bot.config import Settings
from dublin_bot.errors import TransientBrokerError
from dublin_bot.kraken_gateway import KrakenGateway
from dublin_bot.models import Action, Signal

def test_mutation_timeout_is_never_retried():
    g = KrakenGateway.__new__(KrakenGateway)
    g._api_key = "offline-key"
    g._api_secret = base64.b64encode(b"secret").decode()
    g._limiter = Mock()
    g._nonce = Mock()
    g._nonce.next.return_value = 123
    g._request = Mock(side_effect=TransientBrokerError("response lost"))
    g._max_retries = 3
    g._sleep = Mock()
    g._audit = None
    with pytest.raises(TransientBrokerError):
        g._private("AddOrder", {"volume": "1"})
    assert g._request.call_count == 1


def test_same_key_serializes_nonce_through_response(tmp_path, monkeypatch):
    import threading
    import time
    monkeypatch.setenv("HOME", str(tmp_path))
    active = []
    overlap = []
    lock = threading.Lock()
    def request(*a, **k):
        with lock:
            active.append(1)
            if len(active) > 1:
                overlap.append(True)
        time.sleep(.04)
        with lock:
            active.pop()
        return {}
    gs = []
    for _ in range(2):
        g = KrakenGateway.__new__(KrakenGateway)
        g._api_key = "same-offline-key"
        g._api_secret = base64.b64encode(b"secret").decode()
        g._limiter = Mock()
        g._nonce = Mock()
        g._nonce.next.side_effect = lambda: (overlap.append(True) if active else None) or 123
        g._request = request
        g._max_retries = 1
        gs.append(g)
    threads = [threading.Thread(target=g._private, args=("Balance",)) for g in gs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlap

class Exchange:
    def __init__(self):
        self.orders = {}
        self.trades = {}
        self.ledger = {
            "DEPOSIT":dict(refid="deposit",type="deposit",asset="ZUSD",amount="100",fee="0")
        }
        self.sent = []
        self.balance = Decimal("100")
        self.base = Decimal("0")
        self.fail_submit = False
        self.partial = False
        self.race = Decimal("0")
    def resolve_symbol(self, symbol):
        from dublin_bot.kraken_gateway import SymbolMeta
        assert symbol == "BTC/USD"
        return SymbolMeta("XXBTZUSD", "XBTUSD", "XBT/USD", "XXBT", "ZUSD", 8, 1,
                          Decimal(".001"), Decimal(".01"), "online")
    def _assert_can_submit(self): pass
    def get_ticker_for(self, symbol): return {"ask": 100, "last": 100, "bid": 100}
    def fee_schedule(self, pair): return {"taker_bps": 100, "maker_bps": 100}
    def fill(self, oid, qty, price=100):
        o = self.orders[oid]
        qty = Decimal(str(qty))
        price = Decimal(str(price))
        tid = "T" + str(len(self.trades))
        cost = qty * price
        fee = cost / 100
        side = o["descr"]["type"]
        self.trades[tid] = dict(ordertxid=oid, pair="XBTUSD", type=side, vol=str(qty),
                               cost=str(cost), fee=str(fee), price=str(price), time=1700000000 + len(self.trades))
        o["trades"].append(tid)
        for k,v in [("vol_exec",qty),("cost",cost),("fee",fee)]:
            o[k]=str(Decimal(o[k])+v)
        self.ledger[tid+"B"] = dict(refid=tid, type="trade", asset="XXBT", amount=str(qty if side=="buy" else -qty), fee="0")
        self.ledger[tid+"Q"] = dict(refid=tid, type="trade", asset="ZUSD", amount=str(-cost if side=="buy" else cost), fee=str(fee))
        self.base += qty if side=="buy" else -qty
        self.balance += -cost-fee if side=="buy" else cost-fee
        if Decimal(o["vol_exec"]) == Decimal(o["vol"]):
            o["status"]="closed"
    def order(self, oid, side, qty, kind="market", parent=None):
        self.orders[oid] = dict(descr=dict(pair="XBTUSD", type=side, ordertype=kind),
            vol=str(qty), vol_exec="0", cost="0", fee="0", status="open", closetm=1700000010,
            trades=[], refid=parent)
    def _private(self, endpoint, params=None):
        params = params or {}
        if endpoint == "TradeBalance":
            return {"eb":str(self.balance+self.base*100)}
        if endpoint == "BalanceEx":
            return {"ZUSD": {"balance":str(self.balance), "hold_trade":"0"}, "XXBT":{"balance":str(self.base),"hold_trade":"0"}}
        if endpoint == "OpenOrders":
            return {"open": {k:v for k,v in self.orders.items() if v["status"]=="open"}}
        if endpoint == "ClosedOrders":
            rows={k:v for k,v in self.orders.items() if v["status"]!="open"}
            return {"closed":rows,"count":len(rows)}
        if endpoint == "TradesHistory":
            return {"trades":self.trades,"count":len(self.trades)}
        if endpoint == "Ledgers":
            return {"ledger":self.ledger,"count":len(self.ledger)}
        if endpoint == "QueryTrades":
            return {t:self.trades[t] for t in params["txid"].split(",")}
        if endpoint == "QueryOrders":
            return {params["txid"]:self.orders[params["txid"]]}
        if endpoint == "AddOrder":
            self.sent.append(dict(params))
            if self.fail_submit:
                raise TimeoutError("accepted but response lost")
            oid = "O"+str(len(self.sent))
            qty=Decimal(params["volume"])
            self.order(oid,params["type"],qty)
            if params["type"]=="buy":
                filled=qty/2 if self.partial else qty
                self.fill(oid,filled)
                self.order(oid+"S","sell",filled,"stop-loss",oid)
                self.orders[oid]["closetxid"]=oid+"S"
            else:
                self.fill(oid,qty,110)
            return {"txid":[oid]}
        if endpoint == "CancelOrder":
            oid=params["txid"]
            if self.orders[oid]["descr"]["ordertype"]=="stop-loss" and self.race:
                self.fill(oid,self.race,90)
                self.race=Decimal("0")
            self.orders[oid]["status"]="canceled"
            return {"count":1}
        raise AssertionError(endpoint)

def config(tmp_path):
    return Settings(_env_file=None, paper_trading=False,dry_run=False,allow_live_trading=True,
        live_risk_acknowledgement="I_ACCEPT_LIVE_TRADING_RISK", strategy_equity_usd=100,
        session_state_path=str(tmp_path/"session.json"), cooldown_minutes=0, max_position_fraction=.25)

def test_full_buy_owned_sell_and_restart(tmp_path):
    assert hasattr(execution, "RotationExecutor"), "durable live executor missing"
    gw=Exchange()
    s=config(tmp_path)
    path=tmp_path/"live.json"
    ex=execution.RotationExecutor(gw,s,path)
    result=ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))
    assert result["executed"] and ex.holdings()=={"BTC/USD":.25}
    assert gw.sent[0]["pair"]=="XXBTZUSD" and gw.sent[0]["close[ordertype]"]=="stop-loss"
    assert gw.sent[0]["close[price]"]=="96.0"
    ex=execution.RotationExecutor(gw,s,path)
    result=ex.sell("BTC/USD")
    assert result["executed"] and ex.holdings()=={}
    assert Decimal(gw.sent[-1]["volume"])==Decimal(".25")
    assert Decimal(ex.snapshot()["realized"])==Decimal("1.975")
    assert ex.snapshot()["last_exit"]["BTC/USD"]==1700000001
    ex=execution.RotationExecutor(gw,s,path)
    assert ex.snapshot()["last_exit"]["BTC/USD"]==1700000001




def test_agent_live_branches_use_executor(tmp_path,monkeypatch):
    from dublin_bot.agents.trading_agent import TradingAgent
    monkeypatch.chdir(tmp_path)
    gw=Exchange()
    s=config(tmp_path)
    agent=TradingAgent(s,gateway=gw)
    agent.strategy._candidate="BTC/USD"
    result=agent._execute_buy(Signal(Action.BUY,80,"entry",price=100,stop_price=96),{})
    assert result["executed"],result
    assert agent.strategy.get_position()=="BTC/USD"
    assert agent._get_current_holdings()=={"BTC/USD":.25}
    rebuilt=TradingAgent(s,gateway=gw)
    assert rebuilt.strategy.get_position()=="BTC/USD"
    result=rebuilt._execute_sell(Signal(Action.SELL,80,"exit",price=110),{"BTC/USD":.25})
    assert result["executed"],result
    assert rebuilt.strategy.get_position() is None
    assert rebuilt.strategy._last_exit_time["BTC/USD"]==1700000001


def test_partial_entry_multiple_children_cancel_race_exact_remaining(tmp_path):
    gw=Exchange()
    gw.partial=True
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    assert ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))["executed"]
    gw.fill("O1", ".125")
    gw.order("SECOND-STOP","sell",".125","stop-loss","O1")
    # QueryOrders may expose only one child; discover all exact parent refids.
    gw.race=Decimal(".025")
    result=ex.sell("BTC/USD")
    assert result["executed"],result
    assert gw.orders["SECOND-STOP"]["status"]=="canceled"
    assert Decimal(gw.sent[-1]["volume"])==Decimal(".225")
    assert ex.holdings()=={}


def test_stop_fill_reconciles_without_strategy_sell_and_no_double_count(tmp_path):
    gw=Exchange()
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    assert ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))["executed"]
    gw.fill("O1S",".25",90)
    ex.reconcile()
    once=ex.snapshot()
    ex.reconcile()
    assert ex.holdings()=={} and ex.snapshot()["realized"]==once["realized"]
    assert Decimal(once["realized"])==Decimal("-2.975")
    assert len(gw.sent)==1

def test_lost_submit_response_restart_never_duplicates(tmp_path):
    gw=Exchange()
    gw.fail_submit=True
    path=tmp_path/"live.json"
    ex=execution.RotationExecutor(gw,config(tmp_path),path)
    signal=Signal(Action.BUY,80,"entry",price=100,stop_price=96)
    result=ex.buy("BTC/USD",signal)
    assert not result["executed"] and "response lost" in result["error"]
    ex=execution.RotationExecutor(gw,config(tmp_path),path)
    assert "ambiguous" in ex.buy("BTC/USD",signal)["error"]
    assert len(gw.sent)==1 and ex.holdings()=={} and ex.snapshot()["last_fill"]==0

def test_partial_parent_cancel_then_exit(tmp_path):
    gw=Exchange()
    gw.partial=True
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    assert ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))["executed"]
    assert ex.holdings()=={"BTC/USD":.125}
    result=ex.sell("BTC/USD")
    assert result["executed"],result
    assert gw.orders["O1"]["status"]=="canceled"
    assert Decimal(gw.sent[-1]["volume"])==Decimal(".125")

def test_missing_child_blocks_no_sweep(tmp_path):
    gw=Exchange()
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    assert ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))["executed"]
    del gw.orders["O1S"]
    result=ex.sell("BTC/USD")
    assert not result["executed"] and "error" in result and len(gw.sent)==1

@pytest.mark.parametrize("fault",["TradeBalance","TradesHistory","Ledgers","BalanceEx"])
def test_entry_evidence_failure_never_uses_fallback(tmp_path,fault):
    gw=Exchange()
    original=gw._private
    def read(endpoint,params=None):
        if endpoint==fault:
            raise ValueError("authoritative read failed")
        return original(endpoint,params)
    gw._private=read
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    result=ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))
    assert not result["executed"] and not gw.sent

def test_incomplete_history_never_means_empty():
    g=Mock()
    g._private.return_value={"trades":{},"count":5}
    with pytest.raises(ValueError,match="Incomplete"):
        execution.complete_history(g,"TradesHistory","trades")


def test_historical_migration_preview_checks_ledger_balance_and_writes_nothing(tmp_path):
    assert hasattr(execution.RotationExecutor,"preview_migration"),"migration preview missing"
    gw=Exchange()
    gw.order("OKIUZG-6I4Y7-WFYFFZ","buy",".25")
    gw.fill("OKIUZG-6I4Y7-WFYFFZ",".25")
    gw.ledger["DEPOSIT"]={"refid":"deposit","type":"deposit","asset":"ZUSD","amount":"100","fee":"0"}
    path=tmp_path/"live.json"
    ex=execution.RotationExecutor(gw,config(tmp_path),path)
    journal=[{"order_id":"OKIUZG-6I4Y7-WFYFFZ","symbol":"BTC/USD","side":"buy"}]
    risk={"start_equity":"100","peak_equity":"100"}
    preview=ex.preview_migration(journal,risk)
    assert preview["dry_run"] and preview["state"]["lots"]["BTC/USD"]["qty"]=="0.25"
    assert preview["protection_required"]==["BTC/USD"]
    assert not path.exists() and not gw.sent
    gw.base+=Decimal(".01")
    with pytest.raises(ValueError,match="balance"):
        ex.preview_migration(journal,risk)
    assert not path.exists()


def test_cancel_response_loss_reconciles_final_child_before_sell(tmp_path):
    gw=Exchange()
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))
    original=gw._private
    def lost(endpoint, params=None):
        result=original(endpoint,params)
        if endpoint=="CancelOrder":
            raise TimeoutError("cancel response lost")
        return result
    gw._private=lost
    gw.race=Decimal(".025")
    result=ex.sell("BTC/USD")
    assert result["executed"],result
    assert Decimal(gw.sent[-1]["volume"])==Decimal(".225")


def test_same_key_highwater_survives_different_local_nonce_files(tmp_path,monkeypatch):
    monkeypatch.setenv("HOME",str(tmp_path))
    values=[]
    for candidate in (9999999999999999999,1):
        g=KrakenGateway.__new__(KrakenGateway)
        g._api_key="shared-watermark-test"
        g._api_secret=base64.b64encode(b"secret").decode()
        g._limiter=Mock()
        g._nonce=Mock()
        g._nonce.next.return_value=candidate
        g._max_retries=1
        def request(*a, **kw):
            values.append(int(kw["data"]["nonce"]))
            return {}
        g._request=request
        g._private("Balance")
    assert values==[9999999999999999999,10000000000000000000]


def _process_private_call(home, events, key, nonce_file):
    import os
    import time
    from pathlib import Path
    from dublin_bot.nonce import NonceGenerator
    os.environ["HOME"]=home
    g=KrakenGateway.__new__(KrakenGateway)
    g._api_key=key
    g._api_secret=base64.b64encode(b"secret").decode()
    g._limiter=Mock()
    g._nonce=NonceGenerator(Path(nonce_file))
    g._max_retries=1
    def request(*a,**kw):
        with open(events,"a") as f:
            f.write("start "+kw["data"]["nonce"]+"\n")
        time.sleep(.1)
        with open(events,"a") as f:
            f.write("end\n")
        return {}
    g._request=request
    g._private("Balance")

def test_private_serialization_across_processes_preserves_ledgers(tmp_path):
    import multiprocessing
    import json
    ctx=multiprocessing.get_context("spawn")
    events=tmp_path/"events"
    paths=[tmp_path/"a.json",tmp_path/"b.json"]
    high=9999999999999999999
    paths[0].write_text(json.dumps({"last_nonce":high}))
    jobs=[ctx.Process(target=_process_private_call,args=(str(tmp_path),str(events),"process-offline-key",str(path))) for path in paths]
    for job in jobs:
        job.start()
    for job in jobs:
        job.join(10)
        assert job.exitcode==0
    lines=events.read_text().splitlines()
    assert [s.split()[0] for s in lines]==["start","end","start","end"]
    assert int(lines[2].split()[1])>int(lines[0].split()[1])
    assert json.loads(paths[0].read_text())["last_nonce"]>high

def test_pending_partial_exit_blocks_duplicate_until_final_fill(tmp_path):
    gw=Exchange()
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))
    original=gw._private
    def partial_sell(endpoint,params=None):
        if endpoint=="AddOrder" and params["type"]=="sell":
            gw.sent.append(dict(params))
            gw.order("O2","sell",params["volume"])
            gw.fill("O2",".1",110)
            return {"txid":["O2"]}
        return original(endpoint,params)
    gw._private=partial_sell
    assert ex.sell("BTC/USD")["executed"]
    assert ex.holdings()=={"BTC/USD":.15}
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    assert "Pending exit" in ex.sell("BTC/USD")["error"]
    assert len(gw.sent)==2
    gw.fill("O2",".15",110)
    ex.reconcile()
    assert ex.holdings()=={}
    assert not ex.snapshot().get("exit_requested")


def test_live_position_info_never_uses_fallback_account_wrappers(tmp_path):
    from dublin_bot.agents.trading_agent import TradingAgent
    gw=Exchange()
    agent=TradingAgent(config(tmp_path),gateway=gw)
    info=agent.get_position_info()
    assert info["equity"]==100 and info["positions"]==[]
    assert info["realized_pnl_today"] is None


def test_fill_report_contains_exact_order_ids_and_passive_stop_event(tmp_path):
    from dublin_bot.agents.trading_agent import TradingAgent
    gw=Exchange()
    agent=TradingAgent(config(tmp_path),gateway=gw)
    agent.strategy._candidate="BTC/USD"
    result=agent._execute_buy(Signal(Action.BUY,80,"entry",price=100,stop_price=96),{})
    assert result["order_id"]=="O1"
    gw.fill("O1S",".25",90)
    agent.gateway.get_bars_for=Mock(return_value=None)
    report=agent.scan_and_decide()
    assert report["executed"] and report["order_result"]["order_id"]=="O1S"
    assert report["order_result"]["action"]=="SELL" and report["current_position"] is None


def test_exchange_cumulative_fill_regression_blocks_sale(tmp_path):
    gw=Exchange()
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))
    # A stale/contradictory read cannot erase already committed executions.
    gw.orders["O1"].update(vol_exec="0",cost="0",fee="0",trades=[],status="open")
    result=ex.sell("BTC/USD")
    assert not result["executed"] and "regress" in result["error"].lower()
    assert len(gw.sent)==1 and ex.holdings()=={"BTC/USD":.25}


def test_corrupt_owned_quantity_cannot_be_treated_as_flat(tmp_path):
    import json
    gw=Exchange()
    path=tmp_path/"live.json"
    ex=execution.RotationExecutor(gw,config(tmp_path),path)
    ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))
    state=json.loads(path.read_text())
    state["lots"]["BTC/USD"]["qty"]="-1"
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError,match="journal"):
        ex.holdings()


def test_exact_cost_comparison_does_not_roundtrip_average_price(tmp_path):
    gw=Exchange()
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    gw.order("O1","buy",".03")
    gw.fill("O1",".03")
    gw.trades["T0"].update(cost="1",fee=".01")
    gw.ledger["T0Q"].update(amount="-1",fee=".01")
    gw.orders["O1"].update(cost="1",fee=".01")
    record=dict(txid="O1",symbol="BTC/USD",side="buy",params={"volume":".03"},children=[],terminal=False)
    with ex._transaction():
        ex._reconcile_order(record)
    assert ex.holdings()=={"BTC/USD":.03}


def test_unattributed_same_asset_sale_blocks_owned_exit_before_cancel(tmp_path):
    gw=Exchange()
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))
    gw.base+=Decimal("1")  # unrelated owner inventory must not mask an external sale
    gw.order("MANUAL","sell",".1")
    gw.fill("MANUAL",".1")
    result=ex.sell("BTC/USD")
    assert not result["executed"] and "Unattributed" in result["error"]
    assert gw.orders["O1S"]["status"]=="open" and len(gw.sent)==1


def test_empty_trade_history_does_not_override_missing_funding_ledger(tmp_path):
    gw=Exchange()
    gw.ledger={}
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    result=ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))
    assert not result["executed"] and "funding" in result["error"].lower()
    assert not gw.sent


def test_accepted_response_loss_recovers_unique_exact_intent_no_resubmit(tmp_path):
    assert hasattr(execution.RotationExecutor,"recover_pending"),"pending recovery missing"
    import time
    gw=Exchange()
    original=gw._private
    def lost(endpoint,params=None):
        response=original(endpoint,params)
        if endpoint=="AddOrder":
            gw.orders[response["txid"][0]].update(userref=int(params["userref"]),opentm=time.time())
            raise TimeoutError("accepted response lost")
        return response
    gw._private=lost
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    result=ex.buy("BTC/USD",Signal(Action.BUY,80,"entry",price=100,stop_price=96))
    assert not result["executed"] and len(gw.sent)==1
    ex=execution.RotationExecutor(gw,config(tmp_path),tmp_path/"live.json")
    result=ex.recover_pending()
    assert result["executed"] and result["order_id"]=="O1"
    assert ex.holdings()=={"BTC/USD":.25} and len(gw.sent)==1


def test_restart_resumes_reserved_exit_after_child_cancel_before_sell(tmp_path):
    from dublin_bot.agents.trading_agent import TradingAgent
    gw=Exchange()
    agent=TradingAgent(config(tmp_path),gateway=gw)
    agent.strategy._candidate="BTC/USD"
    agent._execute_buy(Signal(Action.BUY,80,"entry",price=100,stop_price=96),{})
    # Crash-like failure before AddOrder intent reservation, after owned stops canceled.
    agent.live_executor._submit=Mock(side_effect=OSError("disk unavailable"))
    failed=agent._execute_sell(Signal(Action.SELL,80,"exit",price=100),{"BTC/USD":.25})
    assert "error" in failed and gw.orders["O1S"]["status"]=="canceled"
    restarted=TradingAgent(config(tmp_path),gateway=gw)
    restarted.gateway.get_bars_for=Mock(return_value=None)
    report=restarted.scan_and_decide()
    assert report["executed"] and restarted._get_current_holdings()=={}
    assert len(gw.sent)==2
