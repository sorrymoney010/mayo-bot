import pandas as pd,numpy as np,json,sys
def load(s,tf):
    d=pd.read_csv(f"data/{s}_15m.csv"); d.index=pd.to_datetime(d.ts,unit="s",utc=True)
    d=d[["open","high","low","close","volume"]].astype(float)
    if tf>15: d=d.resample(f"{tf}min").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
    return d
def ema(x,n): return x.ewm(span=n,adjust=False).mean()
def rsi(c,n=14):
    dl=c.diff(); up=dl.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); dn=(-dl.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+up/dn)
def atr(d,n=14):
    tr=pd.concat([d.high-d.low,(d.high-d.close.shift()).abs(),(d.low-d.close.shift()).abs()],axis=1).max(axis=1)
    return tr.ewm(alpha=1/n,adjust=False).mean()
def signals(d,variant):
    c=d.close; e20,e50,e200=ema(c,20),ema(c,50),ema(c,200); r=rsi(c); a=atr(d)
    hi20=d.high.rolling(20).max().shift(); vr=d.volume/d.volume.rolling(20).mean().shift()
    if variant=="user_breakout":   # Mayo original spec + dual-venue breakout
        s=(e20>e50)&(c>e200)&(r>=45)&(r<=68)&(c>hi20)&(vr>=1.10)
    elif variant=="user_emacross": # kraken-ema-bot spec
        s=(e20>e50)&(e20.shift()<=e50.shift())&(r>=50)&(r<=70)
    elif variant=="pullback_trend": # research: buy pullback to EMA20 in uptrend (maker-friendly)
        s=(e20>e50)&(c>e200)&(d.low<=e20)&(c>e20)&(r>=40)&(r<=60)
    return s.fillna(False),a
def run(d,variant,maker,taker,slip=10,ladder=True,fill_bars=1,maxbars=48):
    s,a=signals(d,variant); o,h,l,c=d.open.values,d.high.values,d.low.values,d.close.values; A=a.values; S=s.values
    trades=[];i=200;n=len(d); miss=0
    while i<n-2:
        if not S[i]: i+=1; continue
        lim=c[i]; risk=1.5*A[i]; j=i+1; filled=None
        for k in range(j,min(j+fill_bars,n)):
            if l[k]<lim: filled=k;break     # must trade through post-only bid
        if filled is None: miss+=1; i+=1; continue
        stop=lim-risk; tps=[lim+risk*m for m in (1,2,3)] if ladder else [lim+2*risk]
        w=[1/3]*3 if ladder else [1.0]; rem=1.0; pnl=0.0; fee=maker; k=filled; hit=0
        while True:
            k+=1
            if k>=n: break
            if l[k]<=stop:   # conservative: stop first
                px=min(stop,o[k]); pnl+=rem*(px*(1-slip/1e4)/lim-1); fee+=rem*taker; rem=0;break
            while hit<len(tps) and h[k]>tps[hit]:
                pnl+=w[hit]*(tps[hit]/lim-1); fee+=w[hit]*maker; rem-=w[hit]; hit+=1
                if ladder and hit==1: stop=lim   # breakeven after TP1
            if rem<=1e-9: break
            if k-filled>=maxbars:
                pnl+=rem*(c[k]*(1-slip/1e4)/lim-1); fee+=rem*taker; rem=0;break
        trades.append((d.index[filled],pnl*1e4-fee,pnl*1e4)); i=k+1
    return trades,miss
def stats(t):
    if not t: return dict(n=0)
    x=np.array([v for _,v,_ in t]); g=np.array([v for *_,v in t])
    pf=x[x>0].sum()/max(-x[x<0].sum(),1e-9)
    return dict(n=len(x),win=round((x>0).mean()*100,1),net_bps=round(x.mean(),1),gross_bps=round(g.mean(),1),pf=round(pf,2))
out=[]
for tf in (15,30,60):
  for v in ("user_breakout","user_emacross","pullback_trend"):
    for fees in ((40,80),(25,40)):
      allt=[];miss=0
      for s in ("PF_XBTUSD","PF_ETHUSD","PF_SOLUSD"):
        t,m=run(load(s,tf),v,*fees,maxbars=int(12*60/tf)); allt+=t; miss+=m
      st=stats(allt); st.update(tf=tf,variant=v,fees=f"{fees[0]}/{fees[1]}",nofill=miss)
      df=pd.DataFrame(allt,columns=["t","net","g"]); df["q"]=df.t.dt.to_period("Q")
      fq=df.groupby("q").net.mean(); st["folds_pos"]=f"{(fq>0).sum()}/{len(fq)}"
      out.append(st); print(st,flush=True)
json.dump(out,open("scorecard.json","w"),indent=1,default=str)
