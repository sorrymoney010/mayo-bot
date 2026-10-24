import pandas as pd,numpy as np,json,sys
sys.path.insert(0,"/workspace/mayo-bot/src")
from dublin_bot.intraday_sleeve import ema,rsi,atr
def load(s):
    d=pd.read_csv(f"data/{s}_15m.csv"); d.index=pd.to_datetime(d.ts,unit="s",utc=True)
    return d[["open","high","low","close","volume"]].astype(float)
def rs(d,tf): return d.resample(f"{tf}min").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
def htf(d15,tf,cols):
    h=rs(d15,tf); f=cols(h)
    f.index=f.index+pd.Timedelta(minutes=tf)-pd.Timedelta(minutes=15)  # available at close of last 15m bar inside it
    return f.reindex(d15.index,method="ffill")
def features(d):
    f1=htf(d,60,lambda h:pd.DataFrame({"h_up":(ema(h.close,20)>ema(h.close,50))&(h.close>ema(h.close,200)),"h_e20":ema(h.close,20),"h_close":h.close}))
    f3=htf(d,30,lambda h:pd.DataFrame({"m_rsi":rsi(h.close),"m_ok":h.close>ema(h.close,20)}))
    a=atr(d); hi=d.high.rolling(20).max().shift(); vr=d.volume/d.volume.rolling(20).mean().shift()
    f=pd.concat([f1,f3],axis=1); f["atr"]=a
    f["sig"]=(f.h_up.astype(bool))&(f.m_ok.astype(bool))&(f.m_rsi.between(45,68))&(d.close>hi)&(vr>=1.10)&(1.5*a/d.close*1e4>=MINR)
    return f
def run(d,maker,taker,slip=10):
    f=features(d); o,h,l,c=(d[x].values for x in "open high low close".split())
    S=f.sig.fillna(False).values; A=f.atr.values; HU=f.h_close.values; HE=f.h_e20.values
    hourclose=((d.index.minute==45)).astype(bool)
    T=[];i=300;n=len(d);miss=0
    while i<n-2:
        if not S[i]: i+=1;continue
        lim=c[i]; r=1.5*A[i]
        if not l[i+1]<lim: miss+=1;i+=1;continue
        k=i+1; stop=lim-r; tps=[lim+r,lim+2*r]; hit=0; rem=1; pnl=0; fee=maker
        while True:
            k+=1
            if k>=n: break
            if l[k]<=stop: px=min(stop,o[k])*(1-slip/1e4); pnl+=rem*(px/lim-1); fee+=rem*taker; rem=0;break
            while hit<2 and h[k]>tps[hit]:
                pnl+=(1/3)*(tps[hit]/lim-1); fee+=maker/3; rem-=1/3; hit+=1
                if hit==1: stop=lim
            if hit>=1 and hourclose[k] and HU[k]<HE[k]:   # runner: 1h close below 1h EMA20
                px=c[k]*(1-slip/1e4); pnl+=rem*(px/lim-1); fee+=rem*taker; rem=0;break
            if k-i>=48: px=c[k]*(1-slip/1e4); pnl+=rem*(px/lim-1); fee+=rem*taker; rem=0;break
        T.append((d.index[i],pnl*1e4-fee,pnl*1e4)); i=k+1
    return T,miss
res=[]
for MINR in (0,100):
  for fees in ((40,80),(22,38)):
    allt=[];miss=0;per={}
    for s in ("PF_XBTUSD","PF_ETHUSD","PF_SOLUSD"):
        t,m=run(load(s),*fees); allt+=t; miss+=m; per[s]=round(np.mean([x[1] for x in t]),1) if t else None
    x=np.array([v[1] for v in allt]); g=np.array([v[2] for v in allt])
    q=pd.Series(x,index=pd.DatetimeIndex([v[0] for v in allt]).tz_localize(None).to_period("Q")).groupby(level=0).mean()
    r=dict(min_risk_bps=MINR,fees=f"{fees[0]}/{fees[1]}",n=len(x),nofill=miss,win=round((x>0).mean()*100,1) if len(x) else 0,
      gross=round(g.mean(),1) if len(x) else 0,net=round(x.mean(),1) if len(x) else 0,pf=round(x[x>0].sum()/max(-x[x<0].sum(),1e-9),2) if len(x) else 0,
      folds=f"{(q>0).sum()}/{len(q)}",per_coin=per); res.append(r); print(r)
json.dump(res,open("scorecard_mtf.json","w"),indent=1)
