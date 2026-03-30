import asyncio, os, time, logging, threading
from datetime import datetime
from flask import Flask, jsonify, request
from iqoptionapi.stable_api import IQ_Option

IQ_EMAIL       = os.environ.get("IQ_EMAIL", "")
IQ_PASSWORD    = os.environ.get("IQ_PASSWORD", "")
MIN_CONFIDENCE = int(os.environ.get("MIN_CONFIDENCE", "75"))
MAX_LOSS_PCT   = float(os.environ.get("MAX_LOSS_PCT", "20"))
MAX_TRADES     = int(os.environ.get("MAX_TRADES", "20"))

TF_BUY    = {"M1": 1,  "M5": 5}
TF_CANDLE = {"M1": 60, "M5": 300}
TF_WAIT   = {"M1": 65, "M5": 305}

OTC_ASSETS  = ["EURUSD-OTC","GBPUSD-OTC","USDJPY-OTC","AUDUSD-OTC","USDCAD-OTC","USDCHF-OTC","NZDUSD-OTC","EURGBP-OTC","EURJPY-OTC","GBPJPY-OTC","AUDJPY-OTC","EURCHF-OTC"]
OPEN_ASSETS = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD","EURGBP","EURJPY","GBPJPY","AUDJPY","EURCHF"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger("BOT")

state = {
    "trades":0,"wins":0,"losses":0,"profit":0.0,"balance":0.0,
    "running":True,"scanning":False,
    "last_signal":None,"active_trade":None,"trade_history":[],
    "account_type":os.environ.get("ACCOUNT_TYPE","PRACTICE"),
    "scan_results":[],"status_msg":"Iniciando...",
    "tf_mode":os.environ.get("TF_MODE","M1"),
    "bet_amount":float(os.environ.get("BET_AMOUNT","3")),
}

def sma(cl,p): return [None if i<p-1 else sum(cl[i-p+1:i+1])/p for i in range(len(cl))]

def rsi(cl,p=14):
    if len(cl)<p+1: return 50
    g=l=0
    for i in range(1,p+1):
        d=cl[i]-cl[i-1]
        if d>=0: g+=d
        else: l-=d
    ag,al=g/p,l/p
    for i in range(p+1,len(cl)):
        d=cl[i]-cl[i-1]
        ag=(ag*(p-1)+(d if d>0 else 0))/p
        al=(al*(p-1)+(-d if d<0 else 0))/p
    return 100 if al==0 else 100-100/(1+ag/al)

def get_patterns(c):
    pats=[]
    if len(c)<3: return pats
    c1,c2,c3=c[-1],c[-2],c[-3]
    b1=abs(c1['close']-c1['open']); r1=c1['max']-c1['min']
    bull1=c1['close']>c1['open']; bull2=c2['close']>c2['open']; bull3=c3['close']>c3['open']
    ls=min(c1['open'],c1['close'])-c1['min']; us=c1['max']-max(c1['open'],c1['close'])
    if r1>0 and b1/r1<0.1: pats.append("neut")
    if ls>b1*2 and us<b1*0.5: pats.append("bull")
    if us>b1*2 and ls<b1*0.5 and not bull1: pats.append("bear")
    if not bull2 and bull1 and c1['open']<=c2['close'] and c1['close']>=c2['open']: pats.append("bull")
    if bull2 and not bull1 and c1['open']>=c2['close'] and c1['close']<=c2['open']: pats.append("bear")
    if bull1 and bull2 and bull3: pats.append("bull")
    if not bull1 and not bull2 and not bull3: pats.append("bear")
    return pats

def analyze(candles, asset=""):
    if len(candles)<10: return {"signal":"AGUARDE","confidence":0,"asset":asset}
    cl=[c['close'] for c in candles]
    m9=sma(cl,9); m21=sma(cl,21); rv=rsi(cl); pats=get_patterns(candles)
    lm9=m9[-1]; lm21=m21[-1]; pm9=m9[-5] if len(m9)>=5 else lm9
    bs=ss=0
    if lm9 and lm21:
        if lm9>lm21 and lm9>pm9: bs+=30
        elif lm9<lm21 and lm9<pm9: ss+=30
    if rv<30: bs+=25
    elif rv>70: ss+=25
    for p in pats:
        if p=="bull": bs+=20
        elif p=="bear": ss+=20
    tot=bs+ss or 1
    if bs>ss and bs>30: return {"signal":"call","confidence":min(95,round(bs/tot*100)),"asset":asset}
    elif ss>bs and ss>30: return {"signal":"put","confidence":min(95,round(ss/tot*100)),"asset":asset}
    return {"signal":"AGUARDE","confidence":0,"asset":asset}

async def run_bot():
    log.info("BOT iniciando")
    api=IQ_Option(IQ_EMAIL,IQ_PASSWORD)
    check,reason=api.connect()
    if not check:
        state['status_msg']=f"Erro: {reason}"; return
    log.info("Conectado!")
    api.change_balance(state['account_type'])
    bal=api.get_balance()
    initial=bal
    state['balance']=bal
    state['status_msg']=f"Conectado | Saldo: ${bal:.2f}"

    while state['running']:
        try:
            bal=api.get_balance()
            state['balance']=bal
            if initial>0 and (initial-bal)/initial*100>=MAX_LOSS_PCT:
                state['status_msg']="🛑 STOP LOSS"; break
            if state['trades']>=MAX_TRADES:
                state['status_msg']=f"✋ Limite atingido"; break

            tf_mode=state['tf_mode']
            tf_buy=TF_BUY.get(tf_mode,1)
            tf_candle=TF_CANDLE.get(tf_mode,60)
            tf_wait=TF_WAIT.get(tf_mode,65)
            bet=state['bet_amount']

            state['scanning']=True
            state['status_msg']=f"Varrendo 24 ativos... ({tf_mode} | ${bet})"
            best=None; results=[]

            for asset in OTC_ASSETS+OPEN_ASSETS:
                try:
                    candles=api.get_candles(asset,tf_candle,50,time.time())
                    if not candles or len(candles)<10: continue
                    r=analyze(candles,asset)
                    if r['signal']!='AGUARDE':
                        results.append(r)
                        if not best or r['confidence']>best['confidence']: best=r
                    await asyncio.sleep(0.3)
                except: continue

            results.sort(key=lambda x:x['confidence'],reverse=True)
            state['scan_results']=results[:10]
            state['scanning']=False

            if not best or best['confidence']<MIN_CONFIDENCE:
                m=best['confidence'] if best else 0
                state['status_msg']=f"Aguardando sinal ≥{MIN_CONFIDENCE}%... (melhor: {m}%)"
                await asyncio.sleep(15); continue

            asset=best['asset']; signal=best['signal']; conf=best['confidence']
            state['status_msg']=f"Entrando: {asset} {signal.upper()} {conf}%"

            # IQ Option buy_digital_spot para binárias com tempo correto
            duration = 1 if tf_mode == "M1" else 5  # minutos
            ok, trade_id = api.buy(bet, asset, signal, duration)
            if not ok:
                log.error("Falha trade")
                await asyncio.sleep(10); continue

            state['trades']+=1
            now=datetime.now().strftime("%H:%M:%S")

            # CAIXA DE ENTRADA ATIVA
            state['active_trade']={
                "time":now,"asset":asset,
                "signal":signal.upper(),"confidence":conf,
                "amount":bet,"tf_mode":tf_mode,"status":"open"
            }
            state['last_signal']={
                "time":now,"asset":asset,"signal":signal.upper(),
                "confidence":conf,"result":"pending","profit":0,
                "tf_mode":tf_mode,"amount":bet
            }
            state['status_msg']=f"⏳ {asset} {signal.upper()} aberto — aguardando {tf_mode}..."
            log.info(f"ENTRADA: {asset} {signal.upper()} ${bet} {tf_mode} {conf}%")
            await asyncio.sleep(tf_wait)

            # Tenta pegar resultado com timeout
            profit = 0
            try:
                result = api.check_win_v3(trade_id)
                profit = float(result) if result else 0
            except:
                log.warning("check_win_v3 falhou — continuando")
                profit = 0
            res="WIN" if profit>0 else "LOSS"
            if profit>0: state['wins']+=1
            else: state['losses']+=1
            state['profit']+=profit
            state['last_signal']['result']=res
            state['last_signal']['profit']=profit
            state['active_trade']['status']=res
            state['active_trade']['profit']=profit

            state['trade_history'].insert(0,{
                "time":datetime.now().strftime("%H:%M:%S"),
                "asset":asset,"signal":signal.upper(),
                "confidence":conf,"result":res,"profit":profit,
                "tf_mode":tf_mode,"amount":bet
            })
            if len(state['trade_history'])>50: state['trade_history'].pop()
            state['balance']=api.get_balance()
            state['active_trade']=None
            log.info(f"{'WIN' if profit>0 else 'LOSS'} ${profit:.2f} | Saldo: ${state['balance']:.2f}")
            await asyncio.sleep(5)
        except KeyboardInterrupt: break
        except Exception as e:
            log.error(f"Erro: {e}"); await asyncio.sleep(10)

app=Flask(__name__)

HTML=r"""<!DOCTYPE html>
<html lang="pt-BR" data-theme="dark">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QUANTEX BOT</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
:root[data-theme="dark"]{
  --bg:#0c0f14;--surface:#131820;--panel:#1a2230;--border:#232f3e;
  --accent:#3b82f6;--green:#10b981;--red:#ef4444;--yellow:#f59e0b;
  --text:#e2e8f0;--text2:#94a3b8;--text3:#475569;--white:#f8fafc;
  --shadow:0 4px 24px rgba(0,0,0,.4);
}
:root[data-theme="light"]{
  --bg:#f0f4f8;--surface:#ffffff;--panel:#f8fafc;--border:#e2e8f0;
  --accent:#2563eb;--green:#059669;--red:#dc2626;--yellow:#d97706;
  --text:#1e293b;--text2:#475569;--text3:#94a3b8;--white:#1e293b;
  --shadow:0 4px 24px rgba(0,0,0,.08);
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;transition:background .3s,color .3s}
.mono{font-family:'DM Mono',monospace}

/* HEADER */
.header{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;box-shadow:var(--shadow)}
.logo{font-size:16px;font-weight:800;letter-spacing:1px;color:var(--accent)}
.logo span{color:var(--green)}
.header-right{display:flex;align-items:center;gap:10px}
.theme-btn{background:none;border:1px solid var(--border);color:var(--text2);padding:5px 10px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .2s}
.theme-btn:hover{border-color:var(--accent);color:var(--accent)}
.acc-badge{font-family:'DM Mono',monospace;font-size:10px;padding:4px 10px;border-radius:6px;border:1px solid;cursor:pointer;transition:all .2s}
.demo-badge{border-color:var(--yellow);color:var(--yellow);background:rgba(245,158,11,.1)}
.real-badge{border-color:var(--red);color:var(--red);background:rgba(239,68,68,.1)}

/* MAIN */
.main{padding:14px;display:flex;flex-direction:column;gap:12px;max-width:480px;margin:0 auto}

/* STATUS */
.status-bar{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px;font-family:'DM Mono',monospace;font-size:10px;color:var(--text2);display:flex;align-items:center;gap:8px}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--green);flex-shrink:0;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.8)}}
.scan-dot{background:var(--accent);animation:pulse .8s infinite}

/* SIGNAL BOX */
.signal-card{border-radius:14px;padding:20px;text-align:center;border:2px solid var(--border);background:var(--surface);transition:all .4s;box-shadow:var(--shadow)}
.signal-card.call{border-color:var(--green);background:linear-gradient(135deg,rgba(16,185,129,.08),rgba(16,185,129,.03));box-shadow:0 0 30px rgba(16,185,129,.15)}
.signal-card.put{border-color:var(--red);background:linear-gradient(135deg,rgba(239,68,68,.08),rgba(239,68,68,.03));box-shadow:0 0 30px rgba(239,68,68,.15)}
.signal-card.wait{border-color:var(--yellow)}
.sig-pair{font-family:'DM Mono',monospace;font-size:11px;color:var(--text3);letter-spacing:2px;margin-bottom:6px}
.sig-dir{font-size:32px;font-weight:800;letter-spacing:2px;margin:4px 0}
.call .sig-dir{color:var(--green)}
.put .sig-dir{color:var(--red)}
.wait .sig-dir{color:var(--yellow);font-size:18px}
.sig-meta{font-family:'DM Mono',monospace;font-size:11px;color:var(--text2);margin-top:4px}
.conf-bar{height:3px;background:var(--border);border-radius:2px;overflow:hidden;margin-top:10px}
.conf-fill{height:100%;border-radius:2px;transition:width .8s ease}
.call .conf-fill{background:var(--green)}.put .conf-fill{background:var(--red)}.wait .conf-fill{background:var(--yellow)}

/* ACTIVE TRADE BOX */
.active-trade{border-radius:12px;padding:14px 16px;border:2px solid var(--accent);background:rgba(59,130,246,.06);display:none}
.at-title{font-size:10px;font-family:'DM Mono',monospace;color:var(--accent);letter-spacing:2px;margin-bottom:8px}
.at-row{display:flex;justify-content:space-between;align-items:center}
.at-asset{font-size:16px;font-weight:700;color:var(--white)}
.at-dir{font-size:14px;font-weight:700;padding:3px 12px;border-radius:6px}
.at-dir.call{background:rgba(16,185,129,.15);color:var(--green)}
.at-dir.put{background:rgba(239,68,68,.15);color:var(--red)}
.at-info{font-family:'DM Mono',monospace;font-size:10px;color:var(--text2);margin-top:6px}
.at-timer{font-family:'DM Mono',monospace;font-size:11px;color:var(--accent);margin-top:4px}

/* STATS */
.stats-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.stats-grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px;text-align:center;box-shadow:var(--shadow)}
.stat-label{font-family:'DM Mono',monospace;font-size:9px;color:var(--text3);letter-spacing:1px;margin-bottom:6px}
.stat-value{font-size:20px;font-weight:800}
.sv-blue{color:var(--accent)}.sv-green{color:var(--green)}.sv-red{color:var(--red)}.sv-yellow{color:var(--yellow)}.sv-white{color:var(--white)}

/* CONTROLS */
.controls-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px;box-shadow:var(--shadow)}
.ctrl-row{display:flex;align-items:center;justify-content:space-between;padding:6px 0}
.ctrl-row+.ctrl-row{border-top:1px solid var(--border);margin-top:6px;padding-top:12px}
.ctrl-label{font-size:13px;font-weight:600;color:var(--text2)}
.tf-btns{display:flex;gap:6px}
.tf-btn{font-family:'DM Mono',monospace;font-size:11px;padding:5px 16px;border-radius:8px;cursor:pointer;border:1px solid var(--border);color:var(--text3);background:var(--bg);transition:all .2s}
.tf-btn.active{border-color:var(--accent);color:var(--accent);background:rgba(59,130,246,.1)}
.bet-ctrl{display:flex;align-items:center;gap:8px}
.bet-btn{width:32px;height:32px;border-radius:8px;cursor:pointer;border:1px solid var(--border);color:var(--text);background:var(--bg);font-size:18px;display:flex;align-items:center;justify-content:center;transition:all .2s;font-weight:300}
.bet-btn:hover{border-color:var(--accent);color:var(--accent)}
.bet-input{font-family:'DM Mono',monospace;font-size:14px;font-weight:500;color:var(--accent);background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:5px 10px;width:70px;text-align:center;outline:none}
.bet-input:focus{border-color:var(--accent)}

/* ACTION BTNS */
.action-btns{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.act-btn{font-size:12px;font-weight:700;padding:12px;border-radius:10px;cursor:pointer;border:1px solid;text-align:center;transition:all .2s;letter-spacing:.5px}
.btn-demo{border-color:var(--yellow);color:var(--yellow);background:rgba(245,158,11,.08)}
.btn-demo:hover{background:var(--yellow);color:#000}
.btn-stop{border-color:var(--red);color:var(--red);background:rgba(239,68,68,.08)}
.btn-stop:hover{background:var(--red);color:#fff}

/* SCAN RESULTS */
.section-title{font-size:11px;font-weight:700;color:var(--text3);letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;font-family:'DM Mono',monospace}
.scan-list{display:flex;flex-direction:column;gap:4px}
.scan-item{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--surface);border-radius:8px;border-left:3px solid var(--border);transition:all .2s}
.scan-item.c{border-left-color:var(--green)}.scan-item.p{border-left-color:var(--red)}
.si-asset{font-size:13px;font-weight:600;color:var(--text)}
.si-dir{font-family:'DM Mono',monospace;font-size:10px;font-weight:500;padding:2px 8px;border-radius:4px}
.si-dir.c{background:rgba(16,185,129,.12);color:var(--green)}
.si-dir.p{background:rgba(239,68,68,.12);color:var(--red)}
.si-conf{font-family:'DM Mono',monospace;font-size:11px;color:var(--text3)}

/* HISTORY */
.history-list{display:flex;flex-direction:column;gap:6px}
.hist-item{background:var(--surface);border-radius:10px;padding:10px 12px;border:1px solid var(--border);border-left:3px solid}
.hist-item.w{border-left-color:var(--green)}.hist-item.l{border-left-color:var(--red)}
.hist-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.hist-asset{font-size:13px;font-weight:700;color:var(--white)}
.hist-result{font-family:'DM Mono',monospace;font-size:12px;font-weight:500}
.hist-item.w .hist-result{color:var(--green)}.hist-item.l .hist-result{color:var(--red)}
.hist-meta{font-family:'DM Mono',monospace;font-size:10px;color:var(--text3)}
.empty{font-family:'DM Mono',monospace;font-size:11px;color:var(--text3);text-align:center;padding:16px}
</style>
</head>
<body>

<div class="header">
  <div class="logo">QUAN<span>TEX</span></div>
  <div class="header-right">
    <button class="theme-btn" onclick="toggleTheme()" id="themeBtn">🌙</button>
    <div class="acc-badge demo-badge" id="accBadge" onclick="toggleAcc()">● DEMO</div>
  </div>
</div>

<div class="main">

  <!-- STATUS -->
  <div class="status-bar">
    <div class="status-dot" id="statusDot"></div>
    <span class="mono" id="statusMsg">Conectando...</span>
  </div>

  <!-- SIGNAL -->
  <div class="signal-card wait" id="signalCard">
    <div class="sig-pair" id="sigPair">AGUARDANDO SINAL</div>
    <div class="sig-dir" id="sigDir">AGUARDE</div>
    <div class="sig-meta" id="sigMeta">Iniciando bot...</div>
    <div class="conf-bar"><div class="conf-fill" id="confFill" style="width:0%"></div></div>
  </div>

  <!-- ACTIVE TRADE -->
  <div class="active-trade" id="activeTrade">
    <div class="at-title">⚡ ENTRADA ATIVA</div>
    <div class="at-row">
      <span class="at-asset" id="atAsset">---</span>
      <span class="at-dir call" id="atDir">CALL</span>
    </div>
    <div class="at-info" id="atInfo">---</div>
    <div class="at-timer" id="atTimer">⏳ Aguardando resultado...</div>
  </div>

  <!-- STATS -->
  <div class="stats-grid">
    <div class="stat-card"><div class="stat-label">SALDO</div><div class="stat-value sv-green" id="statBal">$0</div></div>
    <div class="stat-card"><div class="stat-label">LUCRO</div><div class="stat-value sv-yellow" id="statProfit">+$0</div></div>
    <div class="stat-card"><div class="stat-label">TRADES</div><div class="stat-value sv-blue" id="statTrades">0</div></div>
  </div>
  <div class="stats-grid2">
    <div class="stat-card"><div class="stat-label">✅ WINS</div><div class="stat-value sv-green" id="statWins">0</div></div>
    <div class="stat-card"><div class="stat-label">❌ LOSSES</div><div class="stat-value sv-red" id="statLosses">0</div></div>
  </div>

  <!-- CONTROLS -->
  <div class="controls-card">
    <div class="ctrl-row">
      <span class="ctrl-label">⏱ Timeframe</span>
      <div class="tf-btns">
        <div class="tf-btn active" id="btnM1" onclick="setTF('M1')">M1</div>
        <div class="tf-btn" id="btnM5" onclick="setTF('M5')">M5</div>
      </div>
    </div>
    <div class="ctrl-row">
      <span class="ctrl-label">💵 Entrada</span>
      <div class="bet-ctrl">
        <div class="bet-btn" onclick="changeBet(-1)">−</div>
        <input class="bet-input" id="betInput" type="number" min="1" step="1" value="3" onchange="setBet(this.value)">
        <div class="bet-btn" onclick="changeBet(1)">+</div>
      </div>
    </div>
  </div>

  <!-- ACTION BUTTONS -->
  <div class="action-btns">
    <div class="act-btn btn-demo" onclick="toggleAcc()">🔄 DEMO / REAL</div>
    <div class="act-btn btn-stop" onclick="stopBot()">⏹ PARAR BOT</div>
  </div>

  <!-- SCAN RESULTS -->
  <div>
    <div class="section-title">🔍 Melhores Sinais</div>
    <div class="scan-list" id="scanList"><div class="empty">Aguardando scan...</div></div>
  </div>

  <!-- HISTORY -->
  <div>
    <div class="section-title">📋 Histórico</div>
    <div class="history-list" id="histList"><div class="empty">Sem trades ainda</div></div>
  </div>

</div>

<script>
let isDark = true;
let currentBet = 3;

function toggleTheme(){
  isDark = !isDark;
  document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  document.getElementById('themeBtn').textContent = isDark ? '🌙' : '☀️';
}

async function fetchStatus(){
  try{
    const d = await (await fetch('/api/status')).json();

    // account badge
    const ab = document.getElementById('accBadge');
    const isReal = d.account_type === 'REAL';
    ab.textContent = isReal ? '● REAL' : '● DEMO';
    ab.className = 'acc-badge ' + (isReal ? 'real-badge' : 'demo-badge');

    // status
    document.getElementById('statusMsg').textContent = d.status_msg || '';
    const dot = document.getElementById('statusDot');
    dot.className = 'status-dot' + (d.scanning ? ' scan-dot' : '');

    // signal
    const sig = d.last_signal;
    if(sig && sig.result !== 'pending'){
      const s = sig.signal;
      const card = document.getElementById('signalCard');
      card.className = 'signal-card ' + (s==='CALL'?'call':s==='PUT'?'put':'wait');
      document.getElementById('sigPair').textContent = sig.asset + ' · ' + sig.tf_mode + ' · $' + sig.amount;
      document.getElementById('sigDir').textContent = s==='CALL' ? '▲ CALL' : s==='PUT' ? '▼ PUT' : 'AGUARDE';
      document.getElementById('sigMeta').textContent = sig.confidence + '% confiança · ' + sig.result + ' · ' + sig.time;
      document.getElementById('confFill').style.width = sig.confidence + '%';
    }

    // active trade
    const at = d.active_trade;
    const atBox = document.getElementById('activeTrade');
    if(at && at.status === 'open'){
      atBox.style.display = 'block';
      document.getElementById('atAsset').textContent = at.asset;
      const atDir = document.getElementById('atDir');
      atDir.textContent = at.signal;
      atDir.className = 'at-dir ' + (at.signal==='CALL'?'call':'put');
      document.getElementById('atInfo').textContent = at.tf_mode + ' · $' + at.amount + ' · ' + at.confidence + '% confiança · ' + at.time;
    } else {
      atBox.style.display = 'none';
    }

    // stats
    const bal = d.balance || 0;
    document.getElementById('statBal').textContent = '$' + bal.toFixed(2);
    document.getElementById('statTrades').textContent = d.trades || 0;
    document.getElementById('statWins').textContent = d.wins || 0;
    document.getElementById('statLosses').textContent = d.losses || 0;
    const pv = d.profit || 0;
    const pe = document.getElementById('statProfit');
    pe.textContent = (pv >= 0 ? '+' : '') + '$' + pv.toFixed(2);
    pe.style.color = pv > 0 ? 'var(--green)' : pv < 0 ? 'var(--red)' : 'var(--yellow)';

    // TF buttons
    const tf = d.tf_mode || 'M1';
    document.getElementById('btnM1').className = 'tf-btn' + (tf==='M1'?' active':'');
    document.getElementById('btnM5').className = 'tf-btn' + (tf==='M5'?' active':'');

    // bet
    currentBet = d.bet_amount || 3;
    if(document.activeElement !== document.getElementById('betInput'))
      document.getElementById('betInput').value = currentBet;

    // scan
    const sl = document.getElementById('scanList');
    if(d.scan_results && d.scan_results.length > 0){
      sl.innerHTML = d.scan_results.map(r =>
        `<div class="scan-item ${r.signal==='call'?'c':'p'}">
          <span class="si-asset">${r.asset}</span>
          <span class="si-dir ${r.signal==='call'?'c':'p'}">${r.signal==='call'?'▲ CALL':'▼ PUT'}</span>
          <span class="si-conf">${r.confidence}%</span>
        </div>`).join('');
    }

    // history
    const hl = document.getElementById('histList');
    if(d.trade_history && d.trade_history.length > 0){
      hl.innerHTML = d.trade_history.map(t => {
        const p = t.profit;
        return `<div class="hist-item ${t.result==='WIN'?'w':'l'}">
          <div class="hist-top">
            <span class="hist-asset">${t.asset}</span>
            <span class="hist-result">${t.result==='WIN'?'+'  :''}$${p.toFixed(2)}</span>
          </div>
          <div class="hist-meta">${t.signal} · ${t.tf_mode} · $${t.amount} · ${t.confidence}% · ${t.time}</div>
        </div>`;
      }).join('');
    }
  }catch(e){ console.error(e); }
}

async function setTF(tf){
  await fetch('/api/set-tf',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tf})});
  fetchStatus();
}

async function changeBet(d){
  const newBet = Math.max(1, currentBet + d);
  document.getElementById('betInput').value = newBet;
  await fetch('/api/set-bet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bet:newBet})});
  fetchStatus();
}

async function setBet(val){
  const newBet = Math.max(1, parseFloat(val) || 1);
  document.getElementById('betInput').value = newBet;
  await fetch('/api/set-bet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bet:newBet})});
}

async function toggleAcc(){
  await fetch('/api/toggle',{method:'POST'});
  fetchStatus();
}

async function stopBot(){
  if(!confirm('Parar o bot?')) return;
  await fetch('/api/stop',{method:'POST'});
}

setInterval(fetchStatus, 1000);
fetchStatus();
</script>
</body>
</html>"""

@app.route('/')
def index(): return HTML

@app.route('/api/status')
def status(): return jsonify(state)

@app.route('/api/toggle', methods=['POST'])
def toggle():
    state['account_type'] = 'REAL' if state['account_type']=='PRACTICE' else 'PRACTICE'
    return jsonify({'ok':True})

@app.route('/api/set-tf', methods=['POST'])
def set_tf():
    tf = request.get_json().get('tf','M1')
    if tf in ['M1','M5']: state['tf_mode'] = tf
    return jsonify({'ok':True})

@app.route('/api/set-bet', methods=['POST'])
def set_bet():
    bet = float(request.get_json().get('bet',3))
    state['bet_amount'] = max(1, bet)
    return jsonify({'ok':True})

@app.route('/api/stop', methods=['POST'])
def stop():
    state['running'] = False
    return jsonify({'ok':True})

def start_bot():
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_bot())
        except Exception as e:
            log.error(f"Bot reiniciando: {e}")
            time.sleep(30)

if __name__ == '__main__':
    threading.Thread(target=start_bot, daemon=True).start()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
