import asyncio, os, time, logging, threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string
from iqoptionapi.stable_api import IQ_Option

# CONFIG
IQ_EMAIL       = os.environ.get("IQ_EMAIL", "")
IQ_PASSWORD    = os.environ.get("IQ_PASSWORD", "")
TIMEFRAME      = int(os.environ.get("TIMEFRAME", "60"))
BET_AMOUNT     = float(os.environ.get("BET_AMOUNT", "3"))
MIN_CONFIDENCE = int(os.environ.get("MIN_CONFIDENCE", "75"))
MAX_LOSS_PCT   = float(os.environ.get("MAX_LOSS_PCT", "20"))
MAX_TRADES     = int(os.environ.get("MAX_TRADES", "20"))

OTC_ASSETS = [
    "EURUSD-OTC","GBPUSD-OTC","USDJPY-OTC","AUDUSD-OTC",
    "USDCAD-OTC","USDCHF-OTC","NZDUSD-OTC","EURGBP-OTC",
    "EURJPY-OTC","GBPJPY-OTC","AUDJPY-OTC","EURCHF-OTC",
    "EURAUD-OTC","EURCAD-OTC","GBPAUD-OTC","GBPCHF-OTC",
]
OPEN_ASSETS = [
    "EURUSD","GBPUSD","USDJPY","AUDUSD",
    "USDCAD","USDCHF","NZDUSD","EURGBP",
    "EURJPY","GBPJPY","AUDJPY","EURCHF",
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger("BOT")

state = {
    "trades": 0, "wins": 0, "losses": 0, "profit": 0.0,
    "running": True, "scanning": False,
    "last_signal": None, "trade_history": [],
    "account_type": os.environ.get("ACCOUNT_TYPE", "PRACTICE"),
    "scan_results": [], "status_msg": "Iniciando..."
}

# ANALISE TECNICA
def sma(cl, p):
    return [None if i < p-1 else sum(cl[i-p+1:i+1])/p for i in range(len(cl))]

def rsi(cl, p=14):
    if len(cl) < p+1: return 50
    g = l = 0
    for i in range(1, p+1):
        d = cl[i]-cl[i-1]
        if d >= 0: g += d
        else: l -= d
    ag, al = g/p, l/p
    for i in range(p+1, len(cl)):
        d = cl[i]-cl[i-1]
        ag = (ag*(p-1)+(d if d > 0 else 0))/p
        al = (al*(p-1)+(-d if d < 0 else 0))/p
    return 100 if al == 0 else 100-100/(1+ag/al)

def get_patterns(c):
    pats = []
    if len(c) < 3: return pats
    c1, c2, c3 = c[-1], c[-2], c[-3]
    b1 = abs(c1['close']-c1['open'])
    r1 = c1['max']-c1['min']
    bull1 = c1['close'] > c1['open']
    bull2 = c2['close'] > c2['open']
    bull3 = c3['close'] > c3['open']
    ls = min(c1['open'], c1['close'])-c1['min']
    us = c1['max']-max(c1['open'], c1['close'])
    if r1 > 0 and b1/r1 < 0.1: pats.append("neut")
    if ls > b1*2 and us < b1*0.5: pats.append("bull")
    if us > b1*2 and ls < b1*0.5 and not bull1: pats.append("bear")
    if not bull2 and bull1 and c1['open'] <= c2['close'] and c1['close'] >= c2['open']: pats.append("bull")
    if bull2 and not bull1 and c1['open'] >= c2['close'] and c1['close'] <= c2['open']: pats.append("bear")
    if bull1 and bull2 and bull3: pats.append("bull")
    if not bull1 and not bull2 and not bull3: pats.append("bear")
    return pats

def analyze(candles, asset=""):
    if len(candles) < 10:
        return {"signal": "AGUARDE", "confidence": 0, "asset": asset}
    cl = [c['close'] for c in candles]
    m9 = sma(cl, 9); m21 = sma(cl, 21)
    rv = rsi(cl); pats = get_patterns(candles)
    lm9 = m9[-1]; lm21 = m21[-1]; pm9 = m9[-5] if len(m9) >= 5 else lm9
    bs = ss = 0
    if lm9 and lm21:
        if lm9 > lm21 and lm9 > pm9: bs += 30
        elif lm9 < lm21 and lm9 < pm9: ss += 30
    if rv < 30: bs += 25
    elif rv > 70: ss += 25
    for p in pats:
        if p == "bull": bs += 20
        elif p == "bear": ss += 20
    tot = bs+ss or 1
    if bs > ss and bs > 30:
        return {"signal": "call", "confidence": min(95, round(bs/tot*100)), "asset": asset}
    elif ss > bs and ss > 30:
        return {"signal": "put", "confidence": min(95, round(ss/tot*100)), "asset": asset}
    return {"signal": "AGUARDE", "confidence": 0, "asset": asset}

# BOT
async def run_bot():
    log.info("BOT iniciando — 28 pares OTC + aberto")
    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    check, reason = api.connect()
    if not check:
        state['status_msg'] = f"Erro: {reason}"
        log.error(f"Falha: {reason}")
        return
    log.info("Conectado!")
    api.change_balance(state['account_type'])
    bal = api.get_balance()
    initial = bal
    state['status_msg'] = f"Conectado | Saldo: ${bal:.2f}"
    log.info(f"Saldo: ${bal:.2f}")

    while state['running']:
        try:
            bal = api.get_balance()
            if initial > 0 and (initial-bal)/initial*100 >= MAX_LOSS_PCT:
                state['status_msg'] = "STOP LOSS atingido"
                break
            if state['trades'] >= MAX_TRADES:
                state['status_msg'] = f"Limite {MAX_TRADES} trades"
                break

            # SCAN TODOS OS ATIVOS
            state['scanning'] = True
            all_assets = OTC_ASSETS + OPEN_ASSETS
            state['status_msg'] = f"Varrendo {len(all_assets)} ativos..."
            best = None
            results = []

            for asset in all_assets:
                try:
                    candles = api.get_candles(asset, TIMEFRAME, 50, time.time())
                    if not candles or len(candles) < 10: continue
                    r = analyze(candles, asset)
                    if r['signal'] != 'AGUARDE':
                        results.append(r)
                        log.info(f"  {asset}: {r['signal'].upper()} {r['confidence']}%")
                        if not best or r['confidence'] > best['confidence']:
                            best = r
                    await asyncio.sleep(0.3)
                except:
                    continue

            results.sort(key=lambda x: x['confidence'], reverse=True)
            state['scan_results'] = results[:10]
            state['scanning'] = False

            if not best or best['confidence'] < MIN_CONFIDENCE:
                melhor = best['confidence'] if best else 0
                state['status_msg'] = f"Aguardando sinal forte... (melhor: {melhor}%)"
                await asyncio.sleep(TIMEFRAME)
                continue

            # ENTRADA
            asset = best['asset']
            signal = best['signal']
            conf = best['confidence']
            state['status_msg'] = f"Entrando: {asset} {signal.upper()} {conf}%"
            log.info(f"ENTRADA: {asset} {signal.upper()} ${BET_AMOUNT} {conf}%")

            ok, trade_id = api.buy(BET_AMOUNT, asset, signal, TIMEFRAME)
            if not ok:
                log.error(f"Falha trade")
                await asyncio.sleep(10)
                continue

            state['trades'] += 1
            state['last_signal'] = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "asset": asset, "signal": signal.upper(),
                "confidence": conf, "result": "⏳", "profit": 0
            }
            state['status_msg'] = f"Trade aberto: {asset} {signal.upper()} — aguardando resultado..."
            await asyncio.sleep(TIMEFRAME + 5)

            result = api.check_win_v3(trade_id)
            profit = float(result) if result else 0
            res = "WIN" if profit > 0 else "LOSS"

            if profit > 0: state['wins'] += 1
            else: state['losses'] += 1
            state['profit'] += profit
            state['last_signal']['result'] = res
            state['last_signal']['profit'] = profit
            state['trade_history'].insert(0, {
                "time": datetime.now().strftime("%H:%M:%S"),
                "asset": asset, "signal": signal.upper(),
                "confidence": conf, "result": res, "profit": profit
            })
            if len(state['trade_history']) > 50:
                state['trade_history'].pop()

            log.info(f"{'WIN' if profit>0 else 'LOSS'} ${profit:.2f} | W:{state['wins']} L:{state['losses']}")
            await asyncio.sleep(5)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Erro: {e}")
            await asyncio.sleep(10)

# FLASK APP
app = Flask(__name__)

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/api/status')
def status():
    return jsonify(state)

@app.route('/api/toggle-account', methods=['POST'])
def toggle():
    state['account_type'] = 'REAL' if state['account_type'] == 'PRACTICE' else 'PRACTICE'
    return jsonify({'account_type': state['account_type']})

@app.route('/api/stop', methods=['POST'])
def stop():
    state['running'] = False
    return jsonify({'ok': True})

def start_bot():
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_bot())
        except Exception as e:
            log.error(f"Bot reiniciando: {e}")
            time.sleep(30)

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>QUANTEX BOT</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Share+Tech+Mono&family=Rajdhani:wght@500;700&display=swap');
:root{--bg:#03070d;--panel:#091525;--border:#0d2235;--accent:#00c8ff;--green:#00ff88;--red:#ff3355;--yellow:#ffc200;--dim:#1e3a50;--text:#c8dde8}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Rajdhani',sans-serif;padding:12px}
h1{font-family:'Orbitron',monospace;font-size:18px;color:var(--accent);text-shadow:0 0 20px var(--accent);letter-spacing:3px;text-align:center;margin-bottom:2px}
.sub{text-align:center;font-family:'Share Tech Mono',monospace;font-size:9px;color:var(--dim);margin-bottom:10px;letter-spacing:2px}
.status{text-align:center;font-family:'Share Tech Mono',monospace;font-size:9px;color:var(--accent);margin-bottom:10px;padding:6px;background:var(--panel);border-radius:3px;border:1px solid var(--border)}
.badge{display:inline-block;font-family:'Share Tech Mono',monospace;font-size:9px;padding:3px 10px;border-radius:2px;border:1px solid;margin:0 4px 10px}
.bd{border-color:var(--yellow);color:var(--yellow)}.br{border-color:var(--red);color:var(--red)}
.sb{border-radius:4px;padding:16px;text-align:center;border:2px solid var(--border);background:var(--panel);margin-bottom:12px}
.sb.call{border-color:var(--green);background:#00ff8808;box-shadow:0 0 30px #00ff8820}
.sb.put{border-color:var(--red);background:#ff335508;box-shadow:0 0 30px #ff335520}
.sb.wait{border-color:var(--yellow)}
.sa{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--dim);letter-spacing:2px}
.st{font-family:'Orbitron',monospace;font-size:28px;font-weight:900;letter-spacing:5px;margin:6px 0}
.call .st{color:var(--green);text-shadow:0 0 20px var(--green)}
.put .st{color:var(--red);text-shadow:0 0 20px var(--red)}
.wait .st{color:var(--yellow);font-size:14px}
.sc{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--text)}
.cb{height:4px;background:#03070d;border-radius:3px;overflow:hidden;margin-top:8px}
.cf{height:100%;border-radius:3px}
.call .cf{background:var(--green)}.put .cf{background:var(--red)}.wait .cf{background:var(--yellow)}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:12px;text-align:center}
.cl{font-family:'Share Tech Mono',monospace;font-size:8px;color:var(--dim);letter-spacing:2px;margin-bottom:4px}
.cv{font-family:'Orbitron',monospace;font-size:20px;font-weight:900}
.g{color:var(--green)}.r{color:var(--red)}.y{color:var(--yellow)}.b{color:var(--accent)}
.controls{display:flex;gap:8px;margin-bottom:12px}
.btn{font-family:'Orbitron',monospace;font-size:8px;letter-spacing:1px;padding:9px 12px;border-radius:3px;cursor:pointer;border:1px solid;flex:1;text-align:center}
.bstop{border-color:var(--red);color:var(--red);background:#ff335510}
.bacc{border-color:var(--yellow);color:var(--yellow);background:#ffc20010}
.tt{font-family:'Orbitron',monospace;font-size:8px;letter-spacing:2px;color:var(--dim);margin-bottom:8px}
.sg{display:flex;flex-direction:column;gap:4px;max-height:200px;overflow-y:auto;margin-bottom:12px}
.si{display:flex;justify-content:space-between;padding:5px 8px;background:var(--panel);border-radius:2px;border-left:3px solid var(--border);font-family:'Share Tech Mono',monospace;font-size:10px}
.si.c{border-left-color:var(--green)}.si.p{border-left-color:var(--red)}
.lb{background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:10px;font-family:'Share Tech Mono',monospace;font-size:9px;max-height:160px;overflow-y:auto}
.le{padding:3px 0;border-bottom:1px solid #0d2235;line-height:1.5}
.lw{color:var(--green)}.ll{color:var(--red)}.ld{color:var(--dim)}
</style>
</head>
<body>
<h1>QUANTEX IQ BOT</h1>
<div class="sub">28 PARES · OTC + MERCADO ABERTO</div>
<div style="text-align:center">
  <span class="badge {{acc_class}}">● {{acc_type}}</span>
  {% if scanning %}<span class="badge" style="border-color:var(--accent);color:var(--accent)">🔍 VARRENDO</span>{% endif %}
</div>
<div class="status">{{status_msg}}</div>

<div class="sb {{sig_class}}">
  <div class="sa">{{sig_asset}}</div>
  <div class="st">{{sig_type}}</div>
  <div class="sc">{{sig_conf}}</div>
  <div class="cb"><div class="cf" style="width:{{sig_pct}}%"></div></div>
</div>

<div class="grid">
  <div class="card"><div class="cl">TRADES</div><div class="cv b">{{trades}}</div></div>
  <div class="card"><div class="cl">WINS</div><div class="cv g">{{wins}}</div></div>
  <div class="card"><div class="cl">LOSSES</div><div class="cv r">{{losses}}</div></div>
  <div class="card"><div class="cl">LUCRO</div><div class="cv {{profit_class}}">{{profit_str}}</div></div>
</div>

<div class="controls">
  <a href="/toggle" class="btn bacc">🔄 DEMO / REAL</a>
  <a href="/stop" class="btn bstop">⏹ PARAR</a>
</div>

<div class="tt">🔍 MELHORES SINAIS</div>
<div class="sg">
  {% if scan_results %}
    {% for r in scan_results %}
    <div class="si {{r.cls}}">
      <span>{{r.asset}}</span>
      <span style="color:{{r.color}}">{{r.label}}</span>
      <span style="color:var(--dim)">{{r.confidence}}%</span>
    </div>
    {% endfor %}
  {% else %}
    <div class="ld" style="padding:8px;font-size:10px">Aguardando scan...</div>
  {% endif %}
</div>

<div class="tt">📋 HISTÓRICO</div>
<div class="lb">
  {% if trade_history %}
    {% for t in trade_history %}
    <div class="le {{t.cls}}">[{{t.time}}] {{t.asset}} {{t.signal}} {{t.confidence}}% → {{t.result}} {{t.profit_str}}</div>
    {% endfor %}
  {% else %}
    <div class="le ld">Sem trades ainda</div>
  {% endif %}
</div>

</body>
</html>"""

@app.route('/toggle')
def toggle_acc():
    state['account_type'] = 'REAL' if state['account_type'] == 'PRACTICE' else 'PRACTICE'
    return '<script>history.back()</script>'

@app.route('/stop')
def stop_bot():
    state['running'] = False
    return '<script>history.back()</script>'

def render_page():
    sig = state.get('last_signal')
    if sig and sig.get('result') not in ['⏳', None]:
        s = sig['signal']
        sig_class = 'call' if s == 'CALL' else 'put' if s == 'PUT' else 'wait'
        sig_type = '▲ CALL' if s == 'CALL' else '▼ PUT' if s == 'PUT' else 'AGUARDE'
        sig_asset = sig.get('asset', '---')
        sig_conf = f"{sig.get('confidence',0)}% · {sig.get('result','')} · {sig.get('time','')}"
        sig_pct = sig.get('confidence', 0)
    else:
        sig_class = 'wait'; sig_type = 'AGUARDE'
        sig_asset = 'VARRENDO ATIVOS...'; sig_conf = state['status_msg']
        sig_pct = 0

    profit = state['profit']
    profit_str = ('+' if profit >= 0 else '') + f'${profit:.2f}'
    profit_class = 'g' if profit > 0 else 'r' if profit < 0 else 'y'

    scan_results = []
    for r in state.get('scan_results', []):
        scan_results.append({
            'asset': r['asset'],
            'confidence': r['confidence'],
            'label': '▲ CALL' if r['signal'] == 'call' else '▼ PUT',
            'color': 'var(--green)' if r['signal'] == 'call' else 'var(--red)',
            'cls': 'c' if r['signal'] == 'call' else 'p'
        })

    trade_history = []
    for t in state.get('trade_history', []):
        p = t['profit']
        trade_history.append({**t, 'profit_str': ('+' if p >= 0 else '') + f'${p:.2f}', 'cls': 'lw' if t['result'] == 'WIN' else 'll'})

    from flask import render_template_string as rts
    return rts(HTML,
        acc_type=state['account_type'],
        acc_class='bd' if state['account_type'] == 'PRACTICE' else 'br',
        scanning=state['scanning'],
        status_msg=state['status_msg'],
        sig_class=sig_class, sig_type=sig_type,
        sig_asset=sig_asset, sig_conf=sig_conf, sig_pct=sig_pct,
        trades=state['trades'], wins=state['wins'], losses=state['losses'],
        profit_str=profit_str, profit_class=profit_class,
        scan_results=scan_results, trade_history=trade_history
    )

app.route('/')(lambda: render_page())

if __name__ == '__main__':
    threading.Thread(target=start_bot, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
