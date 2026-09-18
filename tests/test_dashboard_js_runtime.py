"""Offline browser-contract check; fixtures are never served by the dashboard."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest


def test_account_and_saved_history_survive_missing_chart_library():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is not installed')
    js = Path(__file__).resolve().parents[1] / 'src/dublin_bot/static/dashboard.js'
    harness = r'''
const vm = require('node:vm'), fs = require('node:fs');
const nodes = new Map();
function el(id) { if (!nodes.has(id)) nodes.set(id, {textContent:'', innerHTML:'', dataset:{}, classList:{toggle(){}}, disabled:false}); return nodes.get(id); }
const stamp = '2026-09-16T00:00:00Z';
const routes = {
 '/gunbot/stats': {equity:123, cash:17, holdings:[{asset:'BTC', quantity:0.25}], errors:{}, last_updated:stamp},
 '/gunbot/history?offset=0': {fills:[], orders:[], errors:{}, fills_total:0, closed_total:0, last_updated:stamp},
 '/gunbot/saved-fills': {fills:[{order_id:'<script>unsafe</script>',symbol:'BTC/USD',side:'buy',status:'closed',price:10,quantity:1,fee:0,captured_at:stamp}],error:null},
 '/gunbot/health': {engine_status:{state:'process not found',coordinator_pids:[],log_modified_at:null,log_source:'fixture'}}
};
const context={document:{getElementById:el,querySelectorAll:()=>[]},window:{},AbortController,
 setTimeout,clearTimeout,setInterval:()=>0,console,
 fetch:async url=>({ok:true,json:async()=>routes[url]||{error:'unexpected route'}})};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
setTimeout(()=>console.log(JSON.stringify({equity:el('equity').textContent,cash:el('cash').textContent,chartError:el('chart-error').textContent,saved:el('saved-fills').innerHTML})),30);
'''
    result = subprocess.run([node, '-e', harness, str(js)], check=True, capture_output=True, text=True, timeout=5)
    data = json.loads(result.stdout)
    assert data['equity'] == '$123'
    assert data['cash'] == '$17'
    assert 'Local chart library failed to load' in data['chartError']
    assert '&lt;script&gt;unsafe&lt;/script&gt;' in data['saved']
    assert '<script>' not in data['saved']
