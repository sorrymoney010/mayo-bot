"""Fetch Coinbase Exchange + Binance.US public daily candles into $EDGE6_DATA (default /workspace/t6data)."""
import os
os.makedirs(os.environ.get("EDGE6_DATA","/workspace/t6data"),exist_ok=True); os.chdir(os.environ.get("EDGE6_DATA","/workspace/t6data"))
import json, time, urllib.request, datetime as dt, csv, sys
def get(url):
    req=urllib.request.Request(url,headers={"User-Agent":"mayo-research"})
    for i in range(5):
        try: return json.load(urllib.request.urlopen(req,timeout=30))
        except Exception as e: print("retry",e,file=sys.stderr); time.sleep(2)
    raise SystemExit("fail "+url)
def coinbase(prod, start="2016-01-01"):
    s=dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc); rows={}
    end=dt.datetime(2026,11,16,tzinfo=dt.timezone.utc)
    while s<end:
        e=min(s+dt.timedelta(days=299),end)
        d=get(f"https://api.exchange.coinbase.com/products/{prod}/candles?granularity=86400&start={s.isoformat()}&end={e.isoformat()}")
        for t,lo,hi,o,c,v in d: rows[t]=(o,hi,lo,c,v)
        s=e; time.sleep(0.4)
    with open(f"coinbase_{prod}.csv","w") as f:
        w=csv.writer(f); w.writerow(["ts","open","high","low","close","volume"])
        for t in sorted(rows): w.writerow([t,*rows[t]])
    print(prod,len(rows))
def binance(sym, host="api.binance.com"):
    rows={}; st=int(dt.datetime(2017,8,1,tzinfo=dt.timezone.utc).timestamp()*1000)
    while True:
        d=get(f"https://{host}/api/v3/klines?symbol={sym}&interval=1d&startTime={st}&limit=1000")
        if not d: break
        for k in d: rows[k[0]//1000]=(k[1],k[2],k[3],k[4],k[5])
        if len(d)<1000: break
        st=d[-1][0]+1; time.sleep(0.3)
    with open(f"{host.split('.')[1]}_{sym}.csv","w") as f:
        w=csv.writer(f); w.writerow(["ts","open","high","low","close","volume"])
        for t in sorted(rows): w.writerow([t,*rows[t]])
    print(host,sym,len(rows))
for p in ["BTC-USD","ETH-USD"]: coinbase(p)
for s in ["BTCUSDT","ETHUSDT"]:
    try: binance(s)
    except SystemExit as e: print(e)
for s in ["BTCUSD","ETHUSD"]:
    try: binance(s,"api.binance.us")
    except SystemExit as e: print(e)
