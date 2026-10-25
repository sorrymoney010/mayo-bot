"""Binance.US re-run (0 bps maker / 2 bps taker + 3 bps slippage on taker exits) of mtf_synth v1 + v2 families,
purged anchored walk-forward. Run from repo root: python research/intraday/bt4.py"""
import sys,json,itertools,numpy as np,pandas as pd
sys.path.insert(0,"research/intraday"); import bt3
bt3.MK,bt3.TK,bt3.SLIP=0,2,3
from bt3 import feats,portfolio,stats,atr
DATA="/workspace/mayo-intraday/data"
def load(s):
    d=pd.read_csv(f"{DATA}/BUS_{s}_15m.csv"); d.index=pd.to_datetime(d.ts,unit="s",utc=True)
    return d[["open","high","low","close","volume"]].astype(float)
def cands_v1(d,f,P):
    """v1 family: 1h trend + 30m mom (RSI 45-68) + 15m 20-bar breakout; stop k*ATR15m; thirds at r1,r2; BE; runner; ts."""
    MK,TK,SLIP=bt3.MK,bt3.TK,bt3.SLIP
    o,h,l,c=(d[x].values for x in ("open","high","low","close"))
    sig=(f.h_up.astype(bool)&f.m_ok.astype(bool)&f.m_rsi.between(45,68)&f.brk20&(f.vr>=1.10)).fillna(False).values
    A=atr(d).values; HC=f.h_close.values; HE=f.h_e20.values; HEND=f.hour_end.values; out=[];n=len(d)
    for i in np.nonzero(sig)[0]:
        if i<800 or i>=n-2 or not l[i+1]<c[i]: continue
        lim=c[i]; r=P["k"]*A[i]; stop=lim-r; tps=[lim+P["r1"]*r,lim+P["r2"]*r]; hit=0;rem=1.;pnl=0.;fee=MK;k=i+1;why="end"
        while True:
            k+=1
            if k>=n: k=n-1; pnl+=rem*(c[k]/lim-1); fee+=rem*TK; break
            if l[k]<=stop: px=min(stop,o[k])*(1-SLIP/1e4); pnl+=rem*(px/lim-1); fee+=rem*TK; why="stop"; break
            while hit<2 and h[k]>tps[hit]:
                pnl+=(1/3)*(tps[hit]/lim-1); fee+=MK/3; rem-=1/3; hit+=1
                if hit==1: stop=lim
            if hit>=1 and HEND[k] and HC[k]<HE[k]: px=c[k]*(1-SLIP/1e4); pnl+=rem*(px/lim-1); fee+=rem*TK; why="trail"; break
            if k-i>=P["ts"]*4: px=c[k]*(1-SLIP/1e4); pnl+=rem*(px/lim-1); fee+=rem*TK; why="time"; break
        out.append((d.index[i],d.index[k],pnl*1e4-fee,pnl*1e4,why))
    return out
V1BASE=dict(fam="v1",max_open=3,per_day=99,cool=0)
V1GRID=dict(k=(1.5,2.0),r1=(1.0,1.5),r2=(2.0,3.0),ts=(12,24))
V2GRID=dict(bt3.GRID)
def combos():
    c=[dict(V1BASE,**dict(zip(V1GRID,v))) for v in itertools.product(*V1GRID.values())]
    c+=[dict(bt3.BASE,fam="v2",**dict(zip(V2GRID,v))) for v in itertools.product(*V2GRID.values())]
    return c
V1_ORIG=dict(V1BASE,k=1.5,r1=1.0,r2=2.0,ts=12); V2_DEPLOYED=dict(bt3.BASE,fam="v2",entry="breakout",k=2.5,tp=3.0,ts=24,atrx=True)
if __name__=="__main__":
    D={s:load(s) for s in ("BTCUSDT","ETHUSDT","SOLUSDT")}; F={s:feats(D[s]) for s in D}
    idx=D["BTCUSDT"].index; edges=pd.date_range(idx[800],idx[-1],periods=7)
    fold=lambda ts:int(np.searchsorted(edges,ts,side="right")-1)
    def run(P):
        fn=cands_v1 if P["fam"]=="v1" else bt3.cands
        T=portfolio([(s,t) for s in D for t in fn(D[s],F[s],P)],P); return T,[[t for t in T if fold(t[1])==q] for q in range(6)]
    C=combos(); R=[run(P) for P in C]
    def wf(js):
        oos=[];picks=[]
        for q in range(2,6):
            cut=edges[q]-pd.Timedelta(days=3)
            sc=lambda j:(lambda x:x.sum() if len(x)>=10 else -1e18)(np.array([t[3] for t in R[j][0] if t[2]<cut]))
            jb=max(js,key=sc); picks.append(jb); oos+=R[jb][1][q]
        fs=[sum(t[3] for t in oos if fold(t[1])==q) for q in range(2,6)]
        return dict(stats(oos),folds_pos=f"{sum(v>0 for v in fs)}/4",picks=[{k:v for k,v in C[p].items() if k in V1GRID or k in V2GRID or k=="fam"} for p in picks])
    full=lambda T,byf:dict(stats(T),folds_pos=f"{sum(sum(t[3] for t in f)>0 for f in byf)}/6")
    rep={"fees":"binanceus maker0/taker2 + slip3 on taker exits",
         "v1_original_full":full(*run(V1_ORIG)),"v2_kraken_deployed_full":full(*run(V2_DEPLOYED)),
         "wf_v1_family":wf([j for j,P in enumerate(C) if P["fam"]=="v1"]),
         "wf_v2_family":wf([j for j,P in enumerate(C) if P["fam"]=="v2"]),
         "wf_all":wf(range(len(C))),
         "grid_full":[dict({k:v for k,v in P.items() if k in V1GRID or k in V2GRID or k=="fam"},**full(*R[j])) for j,P in enumerate(C)]}
    json.dump(rep,open("research/intraday/scorecard_binanceus.json","w"),indent=1,default=str)
    for k in ("v1_original_full","v2_kraken_deployed_full","wf_v1_family","wf_v2_family","wf_all"): print(k,json.dumps(rep[k],default=str))
    for r in sorted(rep["grid_full"],key=lambda r:-r.get("pf",0))[:12]: print(r)
