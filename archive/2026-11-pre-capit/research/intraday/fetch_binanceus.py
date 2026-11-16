"""Binance.US public 15m klines (no keys). python research/intraday/fetch_binanceus.py"""
import json,time,csv
from urllib.request import Request,urlopen
U="https://api.binance.us/api/v3/klines?symbol={s}&interval=15m&startTime={a}&limit=1000"
OUT="/workspace/mayo-intraday/data"
end=int(time.time()*1000); start=end-400*86400*1000
for s in ["BTCUSDT","ETHUSDT","SOLUSDT"]:
    rows={}; a=start
    while a<end:
        d=json.load(urlopen(Request(U.format(s=s,a=a),headers={"User-Agent":"research"}),timeout=30))
        if not d: break
        for k in d: rows[k[0]//1000]=k[1:6]
        a=d[-1][0]+900000; time.sleep(0.15)
    with open(f"{OUT}/BUS_{s}_15m.csv","w") as f:
        w=csv.writer(f); w.writerow(["ts","open","high","low","close","volume"])
        for t in sorted(rows)[:-1]: w.writerow([t,*rows[t]])
    print(s,len(rows))
