"""Edge4 follow-up (2 more trials, N=529): the limit-entry execution (V5) applied to the FROZEN rule without the
BTC regime filter, on both universes, so execution and filter effects can be separated. No new parameters."""
import sys, json
sys.argv = ["x"]; sys.path.insert(0, "/workspace/mayo-bot/research/edge4")
import refine as R
sg = R.base_signals()
td = R.portfolio(sg, R.START, R.DEV_END, entry="limit"); th = R.portfolio(sg, R.DEV_END, R.HOLD_END, entry="limit")
R.N_TRIALS = R.W["n_total_variants"] + 9
r12 = R.summarize(td, th, "V9_capit_limit_noR2")
print("12coin V9", json.dumps({k: r12[k] for k in ["n_dev", "bus_base", "krk_maker", "krk_mix", "krk_taker", "dsr_krk_mix", "perm_p_krk_mix", "by_year_krk_mix_bps", "net_sum_bps_all_ex3_ex5_days", "holdout"]}, default=str))
json.dump(r12, open("/workspace/mayo-bot/research/edge4/extra_v9.json", "w"), indent=1, default=str)
