# Sourced by the box supervisors (NOT a .env; contains no secrets).
# Exact clean environment for the PAPER loop on the box. Every child is started
# with `env -i "${PAPER_ENV[@]}" ...` so the box shell's Kraken key env vars are
# never inherited. Mirrors scripts/run_paper_mac.sh (main 812a25e).
REPO=/workspace/mayo-bot
# Interpreter: self-healing runtime venv under /workspace/mayo-runtime (see box_venv.sh).
# Supervisors call ensure_venv before every launch; it verifies imports and rebuilds
# (offline, locked) if the venv is missing/broken, falling back to the legacy repo .venv.
source "$REPO/deploy/box/box_venv.sh"
PY="$RUNTIME_PY"
# Process patterns (any interpreter path, so a loop started by an older .venv is still seen).
PAPER_PROC_RE="^[^ ]*/python[^ ]* $REPO/scripts/paper_trader_loop.py"
TICKS_PROC_RE="^[^ ]*/python[^ ]* $REPO/scripts/tick_collector.py --data-dir $REPO/data"
BASE_ENV=(
  PATH=/usr/local/bin:/usr/bin:/bin
  HOME=/home/box
  LANG=C.UTF-8
  PYTHONPATH="$REPO/src"
  PYTHONUNBUFFERED=1
)
PAPER_ENV=(
  "${BASE_ENV[@]}"
  # Non-secret strategy/risk settings the Mac read from its .env (values copied by name;
  # no keys, no secrets, no locks). PAPER_LOOP_SECONDS deliberately NOT set: the loop
  # reads it from the process env only, and the Mac loop ran at the 300 s default.
  BROKER=kraken
  KRAKEN_TIER=starter
  SYMBOL=BTC/USD
  LOOKBACK_BARS=500
  MONITOR_INTERVAL_SECONDS=60
  CANDLE_CLOSE_DELAY_SECONDS=2
  AUTO_START_MONITOR=false
  MAX_DAILY_LOSS_FRACTION=0.03
  MAX_DRAWDOWN_FRACTION=0.10
  MAX_ORDERS_PER_DAY=10
  COOLDOWN_MINUTES=15
  MIN_ORDER_NOTIONAL_USD=1.00
  RAPID_MODE=true
  MAX_BAR_AGE_MULTIPLE=3.0
  MAX_CLOCK_SKEW_SECONDS=30
  MAX_SPREAD_BPS=50
  MIN_DOLLAR_VOLUME=1000000
  HTTP_TIMEOUT_SECONDS=15
  MAX_RETRIES=3
  'BREAKOUT_SYMBOLS=["BTC/USD","ETH/USD","SOL/USD"]'
  BREAKOUT_VOL_AVG=20
  UNIVERSE_MODE=basket
  'UNIVERSE_ALLOWLIST=["BTC/USD","ETH/USD","SOL/USD"]'
  'COIN_BASKET=["BTC/USD","ETH/USD","SOL/USD"]'
  ROTATE_POSITIONS=false
  USE_BRACKET=true
  FAST_EMA=20
  SLOW_EMA=50
  REGIME_EMA=200
  RSI_PERIOD=14
  RSI_MIN=45
  RSI_MAX=68
  ATR_PERIOD=14
  ATR_STOP_MULTIPLIER=1.5
  BREAKOUT_LOOKBACK=20
  VOLUME_LOOKBACK=20
  MIN_VOLUME_RATIO=1.10
  JOURNAL_PATH=logs/decisions.jsonl
  EXECUTION_DB_PATH=logs/executions.sqlite3
  AUDIT_LOG_PATH=logs/audit.jsonl
  IDEMPOTENCY_PATH=logs/orders.json
  NONCE_STATE_PATH=logs/nonce.json
  MARGIN_ENABLED=false
  ADX_PERIOD=14
  ADX_ENTER_ABOVE=25
  ADX_EXIT_BELOW=20
  ADX_GATE_ENABLED=true
  MIN_EDGE_BPS=100
  MIN_EDGE_GATE_ENABLED=true
  LIVE_READY_EQUITY_USD=97
  LIVE_READY_RISK_PER_TRADE=0.01
  LIVE_READY_MAX_CONCURRENT=2
  LIVE_READY_UNIVERSE=BTC/USD,ETH/USD,SOL/USD
  # Wrapper values (scripts/run_paper_mac.sh); safety locks forced.
  PAPER_TRADING=true
  DRY_RUN=true
  ALLOW_LIVE_TRADING=false
  LIVE_RISK_ACKNOWLEDGEMENT=
  MAYO_LEDGER_OWNER=1
  PAPER_BLOCK_PRIVATE_API=true
  PAPER_USE_LEDGER_EQUITY=true
  STRATEGY=regime_trend
  TIMEFRAME_MINUTES=60
  REGIME_LOOKBACK=20
  REGIME_ATR_MULT=3.0
  REGIME_MIN_ATR_RANK=0.0
  STOP_LOSS_PCT=0.03
  TAKE_PROFIT_PCT=0.25
  STRATEGY_EQUITY_USD=500
  RISK_PER_TRADE=0.01
  PAPER_TAKER_FEE_BPS=40
  PAPER_MAKER_FEE_BPS=25
  PAPER_SLIPPAGE_BPS=10
  LEARNER_GATE_ENABLED=true
  LEARNER_MIN_SAMPLE=30
  LEARNER_BENCH_HOURS=72
  MEANREV_SLEEVE_ENABLED=true
  REGIME_DAILY_FILTER=true
  MEANREV_DAILY_FILTER=true
  TRENDHOLD_SLEEVE_ENABLED=true
  TRENDHOLD_POSITION_FRACTION=0.25
  MAX_CONCURRENT_POSITIONS=3
  MAX_POSITION_FRACTION=0.25
  MAX_EXPOSURE_FRACTION=0.75
  PIPELINE_ENABLED=true
  PIPELINE_DATA_DIR="$REPO/data"
  PIPELINE_STALE_SECONDS=600
)
blog() { local f=$1; shift; echo "$(date "+%F %T %Z") $*" >> "$f"; }
# Refuse to run if a .env ever appears in the repo (Settings would read it).
env_file_guard() {
  if [[ -e "$REPO/.env" ]]; then
    blog "$1" "REFUSE: $REPO/.env exists (box must never read a .env); not starting"
    return 1
  fi
  return 0
}
lock_free() { ( exec 7>>"$1"; flock -n 7 ) 9>&- 8>&- 2>/dev/null; }   # 0 = nobody holds it
ensure_health_loop() {
  if lock_free "$REPO/logs/box_health_loop.lock"; then
    setsid nohup "$REPO/deploy/box/health_loop.sh" >/dev/null 2>&1 </dev/null 9>&- 8>&- &
  fi
}
# Python-free supervisor revival (health loop + ensure_box use it; flock makes it idempotent).
ensure_supervisors() {
  local s
  for s in paper ticks; do
    if lock_free "$REPO/logs/box_${s}_supervisor.lock"; then
      setsid nohup "$REPO/deploy/box/run_${s}_supervised.sh" >/dev/null 2>&1 </dev/null 9>&- 8>&- &
    fi
  done
}
# Capped exponential backoff helper: next_backoff <current> -> echoes min(2*current, 300).
next_backoff() { local b=$(( $1 * 2 )); (( b > 300 )) && b=300; echo "$b"; }
# Poll a child instead of a bare `wait` so the supervisor also keeps the health loop alive.
watch_child() {
  local pid=$1 n=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 10 9>&- 8>&-; n=$((n+1))
    if (( n % 30 == 0 )); then ensure_health_loop; fi
  done
  wait "$pid"
}
