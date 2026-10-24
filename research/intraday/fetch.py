import json,time,sys,csv
from urllib.request import Request,urlopen
U="https://futures.kraken.com/api/charts/v1/trade/{s}/15m?from={a}&to={b}"
end=int(time.time()); start=end-365*86400
for s in ["PF_XBTUSD","PF_ETHUSD","PF_SOLUSD"]:
    rows={}; a=start
    while a<end:
        b=min(a+2000*900,end)
        d=json.load(urlopen(Request(U.format(s=s,a=a,b=b),headers={"User-Agent":"research"}),timeout=30))
        for c in d.get("candles",[]): rows[c["time"]//1000]=(c["open"],c["high"],c["low"],c["close"],c["volume"])
        a=b; time.sleep(0.3)
    with open(f"data/{s}_15m.csv","w") as f:
        w=csv.writer(f); w.writerow(["ts","open","high","low","close","volume"])
        for t in sorted(rows): w.writerow([t,*rows[t]])
    print(s,len(rows))
