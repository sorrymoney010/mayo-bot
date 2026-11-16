"""Fee-aware 'fewer, longer trades' redesign of mtf_synth (15m/30m/1h only). Purged walk-forward.
Run from repo root: python research/intraday/bt3.py  (data: /workspace/mayo-intraday/data/PF_*_15m.csv)"""
import pandas as pd,numpy as np,json,sys,itertools
sys.path.insert(0,"src")
from dublin_bot.intraday_sleeve import ema,rsi,atr
DATA="/workspace/mayo-intraday/data"; MK,TK,SLIP=40,80,10
def load(s):
    d=pd.read_csv(f"{DATA}/{s}_15m.csv"); d.index=pd.to_datetime(d.ts,unit="s",utc=True)
    return d[["open","high","low","close","volume"]].astype(float)
def rs(d,tf): return d.resample(f"{tf}min").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
def htf(d15,tf,f):
    h=rs(d15,tf); x=f(h); x.index=x.index+pd.Timedelta(minutes=tf-15); return x.reindex(d15.index,method="ffill")
def feats(d):
    def h1(h):
        e20,e50,e200=ema(h.close,20),ema(h.close,50),ema(h.close,200); a=atr(h)
        return pd.DataFrame({"h_up":(e20>e50)&(h.close>e200),"h_strong":(e50>e50.shift(12))&(e20>e20.shift(6)),
            "h_e20":e20,"h_close":h.close,"h_atr":a,"h_atrx":a>a.rolling(100).mean(),"h_hi":h.high.rolling(10).max().shift()})
    def m30(h): return pd.DataFrame({"m_rsi":rsi(h.close),"m_ok":h.close>ema(h.close,20),"m_e20":ema(h.close,20)})
    f=pd.concat([htf(d,60,h1),htf(d,30,m30)],axis=1)
    a=atr(d); f["vr"]=d.volume/d.volume.rolling(20).mean().shift()
    for n in (20,48): f[f"brk{n}"]=d.close>d.high.rolling(n).max().shift()
    f["hour_end"]=d.index.minute==45
    return f
def cands(d,f,P):
    """every signal -> independent trade outcome (net bps after fees/slip, entry ts, exit ts)."""
    o,h,l,c=(d[x].values for x in ("open","high","low","close"))
    if P.get("entry","breakout")=="breakout":
        sig=(f.h_up.astype(bool)&f.m_ok.astype(bool)&f.m_rsi.between(*P["rsi"])&f[f"brk{P['brk']}"]&(f.vr>=1.10))
    else:  # pullback: 1h uptrend, 15m bar tags the 30m EMA20 and closes back above it
        sig=(f.h_up.astype(bool)&(d.low<=f.m_e20)&(d.close>f.m_e20)&f.m_rsi.between(40,60))
    if P["strong"]: sig&=f.h_strong.astype(bool)
    if P["atrx"]: sig&=f.h_atrx.astype(bool)
    A=f.h_atr.values; HC=f.h_close.values; HE=f.h_e20.values; HEND=f.hour_end.values; S=sig.fillna(False).values
    out=[];n=len(d)
    for i in np.nonzero(S)[0]:
        if i<800 or i>=n-2: continue
        lim=c[i]; r=P["k"]*A[i]
        if r/lim*P["tp"] < P["min_tgt"]: continue          # minimum expected move (first target)
        if not l[i+1]<lim: continue                          # post-only, 15 min, must trade through
        stop=lim-r; tp=lim+P["tp"]*r; rem=1.;pnl=0.;fee=MK;hit=False;k=i+1;why="end"
        while True:
            k+=1
            if k>=n: k=n-1; pnl+=rem*(c[k]/lim-1); fee+=rem*TK; break
            if l[k]<=stop:
                px=min(stop,o[k])*(1-SLIP/1e4); pnl+=rem*(px/lim-1); fee+=rem*TK; why="stop"; break
            if not hit and h[k]>tp:
                pnl+=P["part"]*(tp/lim-1); fee+=MK*P["part"]; rem-=P["part"]; hit=True; stop=max(stop,lim*(1+0.002))
                if rem<=1e-9: why="tp"; break
            if hit and HEND[k] and HC[k]<HE[k]:
                px=c[k]*(1-SLIP/1e4); pnl+=rem*(px/lim-1); fee+=rem*TK; why="trail"; break
            if k-i>=P["ts"]*4:
                px=c[k]*(1-SLIP/1e4); pnl+=rem*(px/lim-1); fee+=rem*TK; why="time"; break
        out.append((d.index[i],d.index[k],pnl*1e4-fee,pnl*1e4,why))
    return out
def portfolio(allc,P):
    allc=sorted(allc,key=lambda x:x[1][0]); open_=[];day={};cool={};T=[]
    for sym,t in allc:
        e,x=t[0],t[1]; open_=[z for z in open_ if z[1]>e]
        if any(z[0]==sym for z in open_) or len(open_)>=P["max_open"]: continue
        if cool.get(sym,e)>e: continue
        dk=e.tz_convert("America/Chicago").date()
        if day.get(dk,0)>=P["per_day"]: continue
        day[dk]=day.get(dk,0)+1; open_.append((sym,x)); T.append((sym,*t))
        if t[2]<0: cool[sym]=x+pd.Timedelta(hours=P["cool"])
    return T
def stats(T):
    if not T: return dict(n=0)
    x=np.array([t[3] for t in T]); g=np.array([t[4] for t in T]); xs=np.sort(x)
    return dict(n=len(x),win=round((x>0).mean()*100,1),gross_bps=round(g.mean(),1),net_bps=round(x.mean(),1),
        pf=round(x[x>0].sum()/max(-x[x<0].sum(),1e-9),2),mean_ex_best2=round(xs[:-2].mean(),1) if len(x)>2 else None,
        avg_hold_h=round(np.mean([(t[2]-t[1]).total_seconds()/3600 for t in T]),1))
BASE=dict(rsi=(50,70),brk=48,strong=True,atrx=False,k=2.5,tp=2.5,part=0.5,min_tgt=0.025,ts=48,max_open=2,per_day=2,cool=12)
GRID=dict(entry=("breakout","pullback"),k=(2.5,3.0),tp=(2.0,3.0),ts=(24,48,72),atrx=(False,True))
if __name__=="__main__":
    D={s:load(s) for s in ("PF_XBTUSD","PF_ETHUSD","PF_SOLUSD")}; F={s:feats(D[s]) for s in D}
    idx=D["PF_XBTUSD"].index; t0=idx[800]; edges=pd.date_range(t0,idx[-1],periods=7)  # 6 folds (~2 months)
    fold=lambda ts:int(np.searchsorted(edges,ts,side="right")-1)
    combos=[dict(BASE,**dict(zip(GRID,v))) for v in itertools.product(*GRID.values())]
    res={}
    for j,P in enumerate(combos):
        allc=[(s,t) for s in D for t in cands(D[s],F[s],P)]; T=portfolio(allc,P)
        byf=[[t for t in T if fold(t[1])==q] for q in range(6)]; res[j]=(P,T,byf)
    # purged anchored walk-forward: pick best PF (min 5 trades) on folds<q with a 3-day purge, test on fold q
    oos=[];picks=[]
    for q in range(2,6):
        cut=edges[q]-pd.Timedelta(days=3)
        def score(j):
            tr=[t for t in res[j][1] if t[2]<cut]; x=np.array([t[3] for t in tr])
            return (x.sum() if len(x)>=5 else -1e18)
        jb=max(res,key=score); picks.append(jb); oos+=res[jb][2][q]
    fs=[np.sum([t[3] for t in f]) for f in [[t for t in oos if fold(t[1])==q] for q in range(2,6)]]
    rep={"wf_oos":dict(stats(oos),folds_pos=f"{sum(v>0 for v in fs)}/4",picks=[{k:combos[p][k] for k in GRID} for p in picks])}
    # stability: fixed configs, full year, 6 folds
    rows=[]
    for j,(P,T,byf) in res.items():
        fp=sum(np.sum([t[3] for t in f])>0 for f in byf)
        rows.append(dict({k:P[k] for k in GRID},**stats(T),folds_pos=f"{fp}/6"))
    rep["grid_full_year"]=rows
    json.dump(rep,open("research/intraday/scorecard_fewer_longer.json","w"),indent=1,default=str)
    print(json.dumps(rep["wf_oos"],default=str))
    for r in sorted(rows,key=lambda r:-r.get("pf",0)): print(r)
