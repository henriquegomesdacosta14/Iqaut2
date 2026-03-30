import asyncio, os, time, logging, threading
from datetime import datetime
from flask import Flask, jsonify, request
from iqoptionapi.stable_api import IQ_Option

IQ_EMAIL       = os.environ.get("IQ_EMAIL", "")
IQ_PASSWORD    = os.environ.get("IQ_PASSWORD", "")
MIN_CONFIDENCE = int(os.environ.get("MIN_CONFIDENCE", "75"))
MAX_LOSS_PCT   = float(os.environ.get("MAX_LOSS_PCT", "20"))
MAX_TRADES     = int(os.environ.get("MAX_TRADES", "20"))

# TF_MODE: "M1" ou "M5"
TF_MODE = os.environ.get("TF_MODE", "M1")

# IQ Option: buy() usa 1=M1, 5=M5 | get_candles() usa 60=M1, 300=M5
TF_BUY    = {"M1": 1,  "M5": 5}
TF_CANDLE = {"M1": 60, "M5": 300}

OTC_ASSETS  = ["EURUSD-OTC","GBPUSD-OTC","USDJPY-OTC","AUDUSD-OTC","USDCAD-OTC","USDCHF-OTC","NZDUSD-OTC","EURGBP-OTC","EURJPY-OTC","GBPJPY-OTC","AUDJPY-OTC","EURCHF-OTC"]
OPEN_ASSETS = ["EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD","EURGBP","EURJPY","GBPJPY","AUDJPY","EURCHF"]

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger("BOT")

state = {
    "trades":0,"wins":0,"losses":0,"profit":0.0,"balance":0.0,
    "running":True,"scanning":False,
    "last_signal":None,"trade_history":[],
    "account_type":os.environ.get("ACCOUNT_TYPE","PRACTICE"),
    "scan_results":[],"status_msg":"Iniciando...",
    "tf_mode": TF_MODE,
    "bet_amount": float(os.environ.get("BET_AMOUNT","3")),
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
    log.info("BOT iniciando — 24 pares OTC + aberto")
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
                state['status_msg']="🛑 STOP LOSS atingido"; break
            if state['trades']>=MAX_TRADES:
                state['status_msg']=f"✋ Limite {MAX_TRADES} trades"; break

            tf_mode = state['tf_mode']
            tf_buy    = TF_BUY.get(tf_mode, 1)
            tf_candle = TF_CANDLE.get(tf_mode, 60)
            tf_wait   = tf_candle  # segundos para aguardar resultado
            bet = state['bet_amount']

            state['scanning']=True
            all_assets=OTC_ASSETS+OPEN_ASSETS
            state['status_msg']=f"🔍 Varrendo {len(all_assets)} ativos... ({tf_mode} | ${bet})"
            best=None; results=[]

            for asset in all_assets:
                try:
                    candles=api.get_candles(asset, tf_candle, 50, time.time())
                    if not candles or len(candles)<10: continue
                    r=analyze(candles,asset)
                    if r['signal']!='AGUARDE':
                        results.append(r)
                        log.info(f"  {asset}: {r['signal'].upper()} {r['confidence']}%")
                        if not best or r['confidence']>best['confidence']: best=r
                    await asyncio.sleep(0.3)
                except: continue

            results.sort(key=lambda x:x['confidence'],reverse=True)
            state['scan_results']=results[:10]
            state['scanning']=False

            if not best or best['confidence']<MIN_CONFIDENCE:
                m=best['confidence'] if best else 0
                state['status_msg']=f"⏳ Aguardando sinal ≥{MIN_CONFIDENCE}%... (melhor: {m}%)"
                await asyncio.sleep(tf_candle); continue

            asset=best['asset']; signal=best['signal']; conf=best['confidence']
            state['status_msg']=f"🚀 {asset} {signal.upper()} {conf}% | {tf_mode} | ${bet}"
            log.info(f"ENTRADA: {asset} {signal.upper()} ${bet} {tf_mode} {conf}%")

            # BUY com timeframe correto (1 para M1, 5 para M5)
            ok,trade_id=api.buy(bet, asset, signal, tf_buy)
            if not ok:
                log.error(f"Falha trade")
                await asyncio.sleep(10); continue

            state['trades']+=1
            state['last_signal']={
                "time":datetime.now().strftime("%H:%M:%S"),
                "asset":asset,"signal":signal.upper(),
                "confidence":conf,"result":"pending","profit":0,
                "tf_mode":tf_mode,"amount":bet
            }
            state['status_msg']=f"⏳ {asset} {signal.upper()} aberto — aguardando {tf_mode}..."
            await asyncio.sleep(tf_wait+5)

            result=api.check_win_v3(trade_id)
            profit=float(result) if result else 0
            res="WIN" if profit>0 else "LOSS"
            if profit>0: state['wins']+=1
            else: state['losses']+=1
            state['profit']+=profit
            state['last_signal']['result']=res
            state['last_signal']['profit']=profit
            state['trade_history'].insert(0,{
                "time":datetime.now().strftime("%H:%M:%S"),
                "asset":asset,"signal":signal.upper(),
                "confidence":conf,"result":res,"profit":profit,
                "tf_mode":tf_mode,"amount":bet
            })
            if len(state['trade_history'])>50: state['trade_history'].pop()
            state['balance']=api.get_balance()
            log.info(f"{'WIN' if profit>0 else 'LOSS'} ${profit:.2f} | Saldo: ${state['balance']:.2f}")
            await asyncio.sleep(5)
        except KeyboardInterrupt: break
        except Exception as e:
            log.error(f"Erro: {e}"); await asyncio.sleep(10)

app=Flask(__name__)

HTML=r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QUANTEX IQ BOT</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Share+Tech+Mono&family=Rajdhani:wght@500;700&display=swap');
:root{--bg:#03070d;--panel:#091525;--border:#0d2235;--accent:#00c8ff;--green:#00ff88;--red:#ff3355;--yellow:#ffc200;--dim:#1e3a50;--text:#c8dde8}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Rajdhani',sans-serif;padding:12px}
h1{font-family:'Orbitron',monospace;font-size:18px;color:var(--accent);text-shadow:0 0 20px var(--accent);letter-spacing:3px;text-align:center;margin-bottom:2px}
.sub{text-align:center;font-family:'Share Tech Mono',monospace;font-size:9px;color:var(--dim);margin-bottom:10px;letter-spacing:2px}
.status{text-align:center;font-family:'Share Tech Mono',monospace;font-size:9px;color:var(--accent);margin-bottom:10px;padding:6px;background:var(--panel);border-radius:3px;border:1px solid var(--border)}
.badge{display:inline-block;font-family:'Share Tech Mono',monospace;font-size:9px;padding:3px 10px;border-radius:2px;border:1px solid;margin:0 4px 10px}
.bd{border-color:var(--yellow);color:var(--yellow)}.br{border-color:var(--red);color:var(--red)}.bs{border-color:var(--accent);color:var(--accent);animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.sb{border-radius:4px;padding:16px;text-align:center;border:2px solid var(--border);background:var(--panel);margin-bottom:12px;transition:all .4s}
.sb.call{border-color:var(--green);background:#00ff8808;box-shadow:0 0 30px #00ff8820}
.sb.put{border-color:var(--red);background:#ff335508;box-shadow:0 0 30px #ff335520}
.sb.wait{border-color:var(--yellow)}
.sa{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--dim);letter-spacing:2px}
.st{font-family:'Orbitron',monospace;font-size:26px;font-weight:900;letter-spacing:5px;margin:6px 0}
.call .st{color:var(--green);text-shadow:0 0 20px var(--green)}
.put .st{color:var(--red);text-shadow:0 0 20px var(--red)}
.wait .st{color:var(--yellow);font-size:14px}
.sc{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--text)}
.cb{height:4px;background:#03070d;border-radius:3px;overflow:hidden;margin-top:8px}
.cf{height:100%;border-radius:3px;transition:width .8s ease}
.call .cf{background:var(--green)}.put .cf{background:var(--red)}.wait .cf{background:var(--yellow)}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:10px;text-align:center}
.cl{font-family:'Share Tech Mono',monospace;font-size:8px;color:var(--dim);letter-spacing:1px;margin-bottom:4px}
.cv{font-family:'Orbitron',monospace;font-size:17px;font-weight:900}
.cg{color:var(--green)}.cr{color:var(--red)}.cy{color:var(--yellow)}.cb2{color:var(--accent)}
.settings{background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:10px;margin-bottom:10px}
.set-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.set-row:last-child{margin-bottom:0}
.set-label{font-family:'Share Tech Mono',monospace;font-size:9px;color:var(--dim)}
.set-btns{display:flex;gap:4px}
.sbtn{font-family:'Share Tech Mono',monospace;font-size:10px;padding:4px 14px;border-radius:2px;cursor:pointer;border:1px solid var(--border);color:var(--dim);background:var(--bg);transition:all .2s}
.sbtn.on{border-color:var(--accent);color:var(--accent);background:#00c8ff15}
.set-ctrl{display:flex;align-items:center;gap:8px}
.sinc{font-size:16px;padding:2px 12px;border-radius:2px;cursor:pointer;border:1px solid var(--border);color:var(--text);background:var(--bg);line-height:1.4}
.sval{font-family:'Orbitron',monospace;font-size:13px;color:var(--accent);min-width:36px;text-align:center}
.controls{display:flex;gap:8px;margin-bottom:12px}
.btn{font-family:'Orbitron',monospace;font-size:8px;letter-spacing:1px;padding:9px;border-radius:3px;cursor:pointer;border:1px solid;flex:1;text-align:center}
.bstop{border-color:var(--red);color:var(--red);background:#ff335510}
.bacc{border-color:var(--yellow);color:var(--yellow);background:#ffc20010}
.tt{font-family:'Orbitron',monospace;font-size:8px;letter-spacing:2px;color:var(--dim);margin-bottom:8px}
.sg{display:flex;flex-direction:column;gap:4px;max-height:160px;overflow-y:auto;margin-bottom:12px}
.si{display:flex;justify-content:space-between;padding:5px 8px;background:var(--panel);border-radius:2px;border-left:3px solid var(--border);font-family:'Share Tech Mono',monospace;font-size:10px}
.si.c{border-left-color:var(--green)}.si.p{border-left-color:var(--red)}
.lb{background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:10px;font-family:'Share Tech Mono',monospace;font-size:9px;max-height:200px;overflow-y:auto}
.le{padding:4px 0;border-bottom:1px solid #0d2235;line-height:1.7}
.lw{color:var(--green)}.ll{color:var(--red)}.ld{color:var(--dim)}
</style>
</head>
<body>
<h1>QUANTEX IQ BOT</h1>
<div class="sub">24 PARES · OTC + MERCADO ABERTO</div>
<div style="text-align:center">
  <span id="ab" class="badge bd">● DEMO</span>
  <span id="sb2" class="badge bs" style="display:none">🔍 VARRENDO</span>
</div>
<div class="status" id="sm">Conectando...</div>

<div id="sigBox" class="sb wait">
  <div class="sa" id="sa">AGUARDANDO SINAL</div>
  <div class="st" id="st">AGUARDE</div>
  <div class="sc" id="sc">Iniciando...</div>
  <div class="cb"><div class="cf" id="cf" style="width:0%"></div></div>
</div>

<div class="grid3">
  <div class="card"><div class="cl">SALDO</div><div class="cv cg" id="bal">$0.00</div></div>
  <div class="card"><div class="cl">LUCRO</div><div class="cv cy" id="pp">+$0.00</div></div>
  <div class="card"><div class="cl">TRADES</div><div class="cv cb2" id="tt">0</div></div>
</div>
<div class="grid2">
  <div class="card"><div class="cl">✅ WINS</div><div class="cv cg" id="ww">0</div></div>
  <div class="card"><div class="cl">❌ LOSSES</div><div class="cv cr" id="ll">0</div></div>
</div>

<div class="settings">
  <div class="set-row">
    <span class="set-label">⏱ TIMEFRAME</span>
    <div class="set-btns">
      <div class="sbtn" id="btnM1" onclick="setTF('M1')">M1</div>
      <div class="sbtn" id="btnM5" onclick="setTF('M5')">M5</div>
    </div>
  </div>
  <div class="set-row">
    <span class="set-label">💵 ENTRADA</span>
    <div class="set-ctrl">
      <div class="sinc" onclick="changeBet(-1)">−</div>
      <span class="sval" id="betVal">$3</span>
      <div class="sinc" onclick="changeBet(1)">+</div>
    </div>
  </div>
</div>

<div class="controls">
  <div class="btn bacc" onclick="toggleAcc()">🔄 DEMO / REAL</div>
  <div class="btn bstop" onclick="stopBot()">⏹ PARAR</div>
</div>

<div class="tt">🔍 MELHORES SINAIS</div>
<div class="sg" id="sg"><div class="ld" style="padding:8px;font-size:10px">Aguardando scan...</div></div>

<div class="tt">📋 HISTÓRICO DE TRADES</div>
<div class="lb" id="lb"><div class="le ld">Sem trades ainda</div></div>

<script>
let currentBet = 3;

async function fetchStatus(){
  try{
    const d=await(await fetch('/api/status')).json();
    // conta
    const ab=document.getElementById('ab');
    ab.textContent=d.account_type==='REAL'?'● REAL':'● DEMO';
    ab.className='badge '+(d.account_type==='REAL'?'br':'bd');
    // scan
    document.getElementById('sb2').style.display=d.scanning?'inline-block':'none';
    // status
    document.getElementById('sm').textContent=d.status_msg||'';
    // signal
    const sig=d.last_signal;
    if(sig && sig.result!=='pending'){
      const s=sig.signal;
      document.getElementById('sigBox').className='sb '+(s==='CALL'?'call':s==='PUT'?'put':'wait');
      document.getElementById('sa').textContent=`${sig.asset||'---'} · ${sig.tf_mode||''} · $${sig.amount||''}`;
      document.getElementById('st').textContent=s==='CALL'?'▲ CALL':s==='PUT'?'▼ PUT':'AGUARDE';
      document.getElementById('sc').textContent=`${sig.confidence||0}% · ${sig.result||''} · ${sig.time||''}`;
      document.getElementById('cf').style.width=(sig.confidence||0)+'%';
    }
    // stats
    const bal=d.balance||0;
    document.getElementById('bal').textContent=`$${bal.toFixed(2)}`;
    document.getElementById('tt').textContent=d.trades||0;
    document.getElementById('ww').textContent=d.wins||0;
    document.getElementById('ll').textContent=d.losses||0;
    const pv=d.profit||0;
    const pe=document.getElementById('pp');
    pe.textContent=(pv>=0?'+':'')+`$${pv.toFixed(2)}`;
    pe.style.color=pv>0?'var(--green)':pv<0?'var(--red)':'var(--yellow)';
    // TF buttons
    const tf=d.tf_mode||'M1';
    document.getElementById('btnM1').className='sbtn'+(tf==='M1'?' on':'');
    document.getElementById('btnM5').className='sbtn'+(tf==='M5'?' on':'');
    // bet
    currentBet=d.bet_amount||3;
    document.getElementById('betVal').textContent=`$${currentBet}`;
    // scan results
    if(d.scan_results&&d.scan_results.length>0){
      document.getElementById('sg').innerHTML=d.scan_results.map(r=>
        `<div class="si ${r.signal==='call'?'c':'p'}">
          <span style="color:var(--text)">${r.asset}</span>
          <span style="color:${r.signal==='call'?'var(--green)':'var(--red)'}">${r.signal==='call'?'▲ CALL':'▼ PUT'}</span>
          <span class="ld">${r.confidence}%</span>
        </div>`).join('');
    }
    // history
    if(d.trade_history&&d.trade_history.length>0){
      document.getElementById('lb').innerHTML=d.trade_history.map(t=>{
        const p=t.profit;
        const emoji=t.result==='WIN'?'✅':'❌';
        return `<div class="le ${t.result==='WIN'?'lw':'ll'}">
          ${emoji} [${t.time}] <strong>${t.asset}</strong> ${t.signal} · ${t.tf_mode} · $${t.amount}
          <br><span class="ld">Confiança: ${t.confidence}% → ${t.result} ${p>=0?'+':''}$${p.toFixed(2)}</span>
        </div>`;
      }).join('');
    }
  }catch(e){console.error(e);}
}

async function setTF(tf){
  await fetch('/api/set-tf',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tf})});
  fetchStatus();
}

async function changeBet(delta){
  const newBet=Math.max(1,currentBet+delta);
  await fetch('/api/set-bet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bet:newBet})});
  fetchStatus();
}

async function toggleAcc(){
  await fetch('/api/toggle',{method:'POST'});
  fetchStatus();
}

async function stopBot(){
  if(!confirm('Parar o bot?'))return;
  await fetch('/api/stop',{method:'POST'});
}

setInterval(fetchStatus,3000);
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
    state['account_type']='REAL' if state['account_type']=='PRACTICE' else 'PRACTICE'
    return jsonify({'ok':True})

@app.route('/api/set-tf', methods=['POST'])
def set_tf():
    data=request.get_json()
    tf=data.get('tf','M1')
    if tf in ['M1','M5']:
        state['tf_mode']=tf
    return jsonify({'ok':True,'tf_mode':state['tf_mode']})

@app.route('/api/set-bet', methods=['POST'])
def set_bet():
    data=request.get_json()
    bet=float(data.get('bet',3))
    state['bet_amount']=max(1,bet)
    return jsonify({'ok':True,'bet_amount':state['bet_amount']})

@app.route('/api/stop', methods=['POST'])
def stop():
    state['running']=False
    return jsonify({'ok':True})

def start_bot():
    while True:
        try:
            loop=asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_bot())
        except Exception as e:
            log.error(f"Bot reiniciando: {e}")
            time.sleep(30)

if __name__=='__main__':
    threading.Thread(target=start_bot,daemon=True).start()
    port=int(os.environ.get('PORT',8080))
    app.run(host='0.0.0.0',port=port)
