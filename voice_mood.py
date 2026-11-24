"""Real-time voice mood analyzer: systray icon + live web page for OBS.

Runs with plain defaults - configuration happens in the browser UI: open
http://127.0.0.1:<port>/ and pick your microphone from the dropdown (the
choice is remembered in settings.json next to this file). The OBS overlay
is the same page; add ?bare to hide the settings panel.

  - tray icon: dot color tracks energy, tooltip shows current mood
  - http://127.0.0.1:<port>/          live page with mic selector
  - http://127.0.0.1:<port>/status    raw JSON

Usage:
    voice-mood\\.venv\\Scripts\\python.exe voice_mood.py [--port 5095]
"""

import argparse
import io
import json
import os
import queue
import threading
import time
import wave

import numpy as np
import opensmile
import sounddevice as sd
from PIL import Image, ImageDraw
import pystray

SR = 16000
CHUNK_SECONDS = 2.0            # mood classification window
FAST_SECONDS = 0.1             # mouth/level refresh window (~10 Hz)
CALIBRATE_SECONDS = 10.0
BLOCK = 1024                   # capture callback block (~64 ms)
MOUTH_ALPHA = 0.5              # smoothing for mouth value per fast window
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "settings.json")
TOKEN_FILE = os.path.join(HERE, "vts_token.json")

STATE = {"mood": "neutral", "f0": 0.0, "spread": 0.0, "loud": 0.0,
         "loud_rel": 0.0, "voiced": 0.0, "db": -60.0, "mouth": 0.0, "ts": ""}
DEV = {"id": None}          # None = system default device
VTS = {"host": ""}          # VTube Studio API host; empty = bridge off
LOCK = threading.Lock()
REOPEN = threading.Event()

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Voice Mood</title>
<style>
 html,body{background:transparent;}
 body{margin:0;font-family:'Segoe UI',system-ui,sans-serif;color:#f2f5fa;}
 /* anchored inside the ~40px OBS dead-zone at canvas edges */
 #wrap{position:fixed;right:48px;bottom:48px;display:flex;flex-direction:column;
  align-items:flex-end;gap:6px;}
 #caption{font-size:13px;font-weight:600;letter-spacing:2px;text-transform:uppercase;
  color:#aeb9c9;text-shadow:0 1px 3px rgba(0,0,0,.8);}
 #card{display:flex;align-items:center;gap:12px;
  background:rgba(8,12,20,.82);border:1px solid rgba(150,170,200,.25);border-radius:999px;
  padding:8px 18px 8px 14px;box-shadow:0 4px 16px rgba(0,0,0,.4);}
 #dot{width:14px;height:14px;border-radius:50%;will-change:transform;}
 #mood{font-size:28px;font-weight:700;letter-spacing:.5px;
  text-shadow:0 2px 4px rgba(0,0,0,.75);transition:color .5s;}
 #sep{width:1px;height:26px;background:rgba(255,255,255,.22);}
 #lvl{display:flex;align-items:center;gap:8px;}
 #meter{width:70px;height:6px;border-radius:3px;background:rgba(255,255,255,.18);overflow:hidden;}
 #fill{height:100%;width:10%;border-radius:3px;background:#8cc0ff;transition:width .25s;}
 #db{font-size:15px;font-weight:600;color:#cfd9e8;text-shadow:0 1px 3px rgba(0,0,0,.8);min-width:52px;}
 #panel{position:fixed;left:16px;top:16px;}
 select{background:#151a24;color:#cfd8e6;border:1px solid #2a3342;border-radius:6px;
        padding:6px 8px;font-size:13px;max-width:260px;}
 body.bare #panel{display:none;}
</style></head><body>
<div id="wrap">
 <div id="caption">voice mood</div>
 <div id="card">
  <div id="dot" style="background:#9aa5b5"></div>
  <div id="mood">neutral</div>
  <div id="sep"></div>
  <div id="lvl"><div id="meter"><div id="fill"></div></div><div id="db">-</div></div>
 </div>
</div>
<div id="panel">
 <select id="mic" onchange="pick(this.value)"></select>
 <input id="vts" placeholder="VTuber host, e.g. 192.168.15.16" onchange="setVts(this.value)">
</div>
<script>
const COLORS={excited:'#f2b134',cheerful:'#ffd166',focused:'#4ea1f7',calm:'#6fd3c7',tired:'#7a86c8',neutral:'#9aa5b5',idle:'#5b6472'};
let target=0.2,cur=0.2,phase=0,last=0;
function fillSel(s){
 const sel=document.getElementById('mic');
 if(sel.dataset.sig!==JSON.stringify(s.devices)){
  sel.dataset.sig=JSON.stringify(s.devices);
  sel.innerHTML='';
  const opts=[['-1','System default']].concat(s.devices.map(d=>[String(d.index),d.name]));
  for(const [val,name] of opts){
   const o=document.createElement('option');o.value=val;o.textContent=name;sel.appendChild(o);
  }
 }
 sel.value=String(s.device);
}
async function pick(v){
 await fetch('/device',{method:'POST',body:JSON.stringify({device:v==='-1'?null:+v})});
 setTimeout(tick,300);
}
async function setVts(v){
 await fetch('/vts',{method:'POST',body:JSON.stringify({host:v})});
}
async function tick(){
 try{
  const r=await fetch('/status',{cache:'no-store'}); const s=await r.json();
  document.body.className=location.search.indexOf('bare')>=0?'bare':'';
  const c=COLORS[s.mood]||'#9aa5b5';
  const mood=document.getElementById('mood');
  mood.textContent=s.mood; mood.style.color=c;
  const dot=document.getElementById('dot');
  dot.style.background=c; dot.style.boxShadow='0 0 10px '+c;
  // input level: bar = rms dB mapped -60..0, number shown alongside
  const dbv=(s.db!==undefined)?s.db:-60;
  const lvl=Math.max(0,Math.min(1,(dbv+60)/60));
  document.getElementById('fill').style.width=Math.round(lvl*100)+'%';
  document.getElementById('fill').style.background=c;
  document.getElementById('db').textContent=dbv.toFixed(1)+' dB';
  target=Math.max(0.08,Math.min(1,(s.voiced||0)*0.8+Math.max(0,s.loud_rel||0)*0.06));
  fillSel(s);
  const vts=document.getElementById('vts');
  if(vts&&document.activeElement!==vts)vts.value=s.vts||'';
 }catch(e){}
 setTimeout(tick,1000);
}
function frame(now){
 const dt=Math.min(0.06,((now-last)/1000)||0.016);last=now;
 cur+=(target-cur)*Math.min(1,dt*5);          // glide toward last reading
 phase+=dt*(1.6+cur*2.5);                      // breathe faster when active
 const breathe=0.5+0.5*Math.sin(phase*2.1);
 const scale=0.85+cur*(0.25+0.35*breathe);     // quiet = still, talking = pulse
 const dot=document.getElementById('dot');
 if(dot)dot.style.transform='scale('+scale.toFixed(3)+')';
 requestAnimationFrame(frame);
}
tick();requestAnimationFrame(frame);
</script></body></html>"""

DEMO = """<!doctype html><html><head><meta charset="utf-8"><title>Voice Mood — demo</title>
<style>
 html,body{background:transparent;}
 body{margin:0;font-family:'Segoe UI',system-ui,sans-serif;color:#f2f5fa;}
 #wrap{position:fixed;right:48px;bottom:48px;display:flex;flex-direction:column;align-items:flex-end;gap:6px;}
 #caption{font-size:13px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:#aeb9c9;text-shadow:0 1px 3px rgba(0,0,0,.8);}
 #card{display:flex;align-items:center;gap:12px;background:rgba(8,12,20,.82);border:1px solid rgba(150,170,200,.25);border-radius:999px;padding:8px 18px 8px 14px;box-shadow:0 4px 16px rgba(0,0,0,.4);}
 #dot{width:14px;height:14px;border-radius:50%;will-change:transform;}
 #mood{font-size:28px;font-weight:700;letter-spacing:.5px;text-shadow:0 2px 4px rgba(0,0,0,.75);transition:color .6s;}
 #sep{width:1px;height:26px;background:rgba(255,255,255,.22);}
 #lvl{display:flex;align-items:center;gap:8px;}
 #meter{width:70px;height:6px;border-radius:3px;background:rgba(255,255,255,.18);overflow:hidden;}
 #fill{height:100%;width:10%;border-radius:3px;background:#8cc0ff;}
 #db{font-size:15px;font-weight:600;color:#cfd9e8;text-shadow:0 1px 3px rgba(0,0,0,.8);min-width:52px;}
</style></head><body>
<div id="wrap">
 <div id="caption">voice mood</div>
 <div id="card">
  <div id="dot"></div>
  <div id="mood">neutral</div>
  <div id="sep"></div>
  <div id="lvl"><div id="meter"><div id="fill"></div></div><div id="db">-30.0 dB</div></div>
 </div>
</div>
<script>
// scripted cycle through every state so all looks are visible without talking:
// [name, color, energy] — energy drives dot breathing + level bar
const ST=[['neutral','#9aa5b5',0.45],['focused','#4ea1f7',0.6],['cheerful','#ffd166',0.7],['excited','#f2b134',0.9],['calm','#6fd3c7',0.35],['tired','#7a86c8',0.25],['idle','#5b6472',0.1]];
let i=0,cur=ST[0][2],phase=0,last=performance.now();
function step(){
 const s=ST[i];
 document.getElementById('mood').textContent=s[0];
 document.getElementById('mood').style.color=s[1];
 const dot=document.getElementById('dot');
 dot.style.background=s[1]; dot.style.boxShadow='0 0 10px '+s[1];
 document.getElementById('fill').style.background=s[1];
 cur=s[2];
}
function frame(now){
 const dt=Math.min(0.06,((now-last)/1000)||0.016);last=now;
 const s=ST[i];
 const wobble=s[2]*0.3*Math.sin(now/300+i);   // level jitter while active
 const lvl=Math.max(0,Math.min(1,s[2]+wobble));
 document.getElementById('fill').style.width=Math.round(lvl*100)+'%';
 document.getElementById('db').textContent=(-42+lvl*42).toFixed(1)+' dB';
 phase+=dt*(1.6+cur*2.5);
 const breathe=0.5+0.5*Math.sin(phase*2.1);
 const scale=0.85+cur*(0.25+0.35*breathe);
 document.getElementById('dot').style.transform='scale('+scale.toFixed(3)+')';
 requestAnimationFrame(frame);
}
step();setInterval(function(){i=(i+1)%ST.length;step();},5000);requestAnimationFrame(frame);
</script></body></html>"""


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float32) ** 2)) + 1e-9)


def extract(smile, chunk):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(chunk, -1, 1) * 32767).astype("<i2").tobytes())
    buf.seek(0)
    df = smile.process_file(buf)
    row = df.iloc[0]

    def pick(prefix):
        col = next((c for c in df.columns if c.startswith(prefix)), None)
        try:
            return float(row[col]) if col else 0.0
        except (TypeError, ValueError):
            return 0.0

    return pick("loudness_sma3"), pick("F0semitoneFrom27.5Hz_sma3nz")


def pitch_spread(chunk):
    """Variability of autocorrelation F0 over voiced frames, in semitones."""
    frame, hop = 1024, 512
    f0s = []
    for start in range(0, len(chunk) - frame, hop):
        seg = chunk[start:start + frame].astype(np.float32)
        seg = seg - seg.mean()
        if rms(seg) < 0.01:
            continue
        corr = np.correlate(seg, seg, "full")[frame - 1:]
        if corr[0] <= 0:
            continue
        lo, hi = int(SR / 400), max(int(SR / 80), 2)
        w = corr[lo:hi]
        if len(w) == 0:
            continue
        f0s.append(SR / (int(np.argmin(w)) + lo))
    if len(f0s) < 3:
        return 0.0
    a = np.log2(np.array(f0s)) * 12.0
    return float(np.percentile(a, 75) - np.percentile(a, 25))


def voiced_ratio(chunk):
    hop = 512
    n = voiced = 0
    for s in range(0, len(chunk) - hop, hop):
        n += 1
        if rms(chunk[s:s + hop]) > 0.012:
            voiced += 1
    return voiced / max(n, 1)


COLORS = {"excited": "#f2b134", "cheerful": "#ffd166", "focused": "#4ea1f7",
          "calm": "#6fd3c7", "tired": "#7a86c8", "neutral": "#9aa5b5",
          "idle": "#5b6472"}
SMOOTH = {}                    # EMA-smoothed z-scores + voiced ratio
REF = {}                       # per-feature running center and deviation
PENDING = [None, 0]            # candidate label + consecutive-chunk repeats
ALPHA = 0.35                   # EMA weight per chunk
HOLD_MIN = 2                   # repeats needed before a label change commits
ADAPT = 0.04                   # speed of the per-speaker reference drift
SILENT_CHUNKS = [0]            # consecutive near-silent chunks (mute check)
IDLE_AFTER = 3                 # chunks of silence before "idle" is shown

# Deviation floors so the z-score denominator can't collapse (units per feature)
FLOOR = {"loud": 0.8, "pitch": 2.0, "spread": 1.2}


def zscore(key, value):
    """Standardize against this speaker's own running history so labels track
    how THEY vary, instead of fixed thresholds most voices never reach."""
    mean, dev = REF.get(key, (value, FLOOR[key]))
    mean += ADAPT * (value - mean)
    dev = max(dev + ADAPT * (abs(value - mean) - dev), FLOOR[key])
    REF[key] = (mean, dev)
    return (value - mean) / dev


def candidate(ze, zp, zsp, voiced):
    """Seven-label taxonomy over the speaker's own z-scored baseline:
    arousal (loudness/talk-time) crossed with expressiveness (pitch height
    and intonation variety). Ordering matters — most specific first.
    Thresholds are deliberately low: 'neutral' should be the quiet-room
    fallback, not where most speech lands."""
    energetic = ze > 0.4 or voiced > 0.65
    bright = zp > 0.25
    lively = zsp > 0.4
    if energetic and lively:
        return "excited"                 # loud, fast, wide melody
    if energetic and bright:
        return "cheerful"                # bright and up-tempo, steadier melody
    if voiced > 0.5 and ze > -0.6:
        return "focused"                 # steady stream of words
    if bright and voiced > 0.3:
        return "cheerful"                # light lifted tone
    if lively and voiced > 0.35:
        return "cheerful"
    if voiced > 0.35:
        return "focused"                 # any real talking beats neutral
    if ze < -0.7:
        return "tired"                   # quiet, below own norm
    if ze < -0.3:
        return "calm"                    # gentle, unhurried
    return "neutral"


def mouth_open(db):
    """0..1 lip openness from input dBFS for VTuber rigs (-50 closes, -10 opens)."""
    return max(0.0, min(1.0, (db + 50.0) / 40.0))


def load_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f).get("token")
    except (OSError, ValueError):
        return None


def save_token(tok):
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": tok}, f)
    except OSError:
        pass


def vts_bridge():
    """Keep VTube Studio MouthOpen synced with the smoothed mouth value.

    One process for everything: instead of running vtbridge.py separately,
    the main app injects directly when a VTS host is set in the web panel.
    """
    from websocket import create_connection

    def msg(mtype, data=None):
        m = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
             "requestID": "voicemood", "messageType": mtype}
        if data is not None:
            m["data"] = data
        return json.dumps(m)

    ws = None
    while True:
        host = VTS["host"]
        if not host:
            time.sleep(0.5)
            continue
        try:
            if ws is None:
                ws = create_connection(f"ws://{host}:8001", timeout=5)
                token = load_token()
                if not token:
                    ws.send(msg("AuthenticationTokenRequest",
                                {"pluginName": "VoiceMood",
                                 "pluginDeveloper": "redbelter"}))
                    token = json.loads(ws.recv())["data"]["authenticationToken"]
                    save_token(token)
                ws.send(msg("AuthenticationRequest",
                            {"pluginName": "VoiceMood",
                             "pluginDeveloper": "redbelter",
                             "authenticationToken": token}))
                ws.recv()
            with LOCK:
                mouth = STATE["mouth"]
            ws.send(msg("InjectParameterDataRequest", {
                "faceFound": False, "mode": "set",
                "parameterValues": [{"id": "MouthOpen",
                                     "value": round(mouth, 3)}]}))
            ws.recv()
        except Exception:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(1)
        time.sleep(0.08)


def stable_update(loud, pitch, spread, voiced, db, ts):
    """Feed one chunk: z-standardize, EMA-smooth, hold through pauses, commit
    a label change only after HOLD_MIN consecutive agreeing chunks. Sustained
    silence shows 'idle' so a muted/unplugged mic is visible on the overlay."""
    if voiced < 0.10:                       # pause — hold readings, count silence
        SILENT_CHUNKS[0] += 1
        with LOCK:
            STATE.update(ts=ts)
            if SILENT_CHUNKS[0] >= IDLE_AFTER and STATE["mood"] != "idle":
                STATE.update(mood="idle")
        return
    SILENT_CHUNKS[0] = 0
    ze = zscore("loud", loud)
    zp = zscore("pitch", pitch)
    zsp = zscore("spread", spread)
    if not SMOOTH:
        SMOOTH.update(l=ze, p=zp, s=zsp, v=voiced)
    else:
        for k, v in (("l", ze), ("p", zp), ("s", zsp), ("v", voiced)):
            SMOOTH[k] += ALPHA * (v - SMOOTH[k])
    cand = candidate(SMOOTH["l"], SMOOTH["p"], SMOOTH["s"], SMOOTH["v"])
    if cand == PENDING[0]:
        PENDING[1] += 1
    else:
        PENDING[:] = [cand, 1]
    with LOCK:
        STATE.update(f0=pitch, spread=spread, loud=loud,
                     loud_rel=SMOOTH["l"] * 4, voiced=SMOOTH["v"], ts=ts)
        if PENDING[1] >= HOLD_MIN and cand != STATE["mood"]:
            STATE.update(mood=cand)


def input_devices():
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append({"index": i, "name": d["name"]})
    return out


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
        dev = data.get("device")
        if isinstance(dev, int):
            DEV["id"] = dev
        host = data.get("vts")
        if isinstance(host, str) and host:
            VTS["host"] = host
    except (OSError, ValueError):
        pass


def save_settings():
    try:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump({"device": DEV["id"], "vts": VTS["host"]}, f)
    except OSError:
        pass


def set_device(value):
    DEV["id"] = None if value in (None, "", -1) else int(value)
    save_settings()
    REOPEN.set()


def set_vts(host):
    VTS["host"] = (host or "").strip()
    save_settings()


def icon_image(hex_color):
    """Mini microphone glyph: dark disc, colored capsule mic with grille,
    cradle arc and stem — ring takes the current mood color."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(12, 16, 24, 235))
    d.ellipse((2, 2, 62, 62), outline=hex_color, width=3)
    # mic capsule
    d.rounded_rectangle((24, 10, 40, 37), radius=8, fill=hex_color)
    # cradle arc, stem, foot (bold so they survive tiny sizes)
    d.arc((20, 24, 44, 46), start=195, end=345, fill=(255, 255, 255, 240), width=3)
    d.line((32, 41, 32, 50), fill=(255, 255, 255, 240), width=3)
    d.line((23, 50, 41, 50), fill=(255, 255, 255, 240), width=3)
    return img


def build_handler():
    from http.server import BaseHTTPRequestHandler

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/demo"):
                self._send(DEMO.encode(), "text/html; charset=utf-8")
                return
            with LOCK:
                payload = dict(STATE)
            payload["devices"] = input_devices()
            payload["device"] = DEV["id"] if DEV["id"] is not None else -1
            payload["vts"] = VTS["host"]
            if self.path.startswith("/status"):
                self._send(json.dumps(payload).encode(), "application/json")
            else:
                self._send(PAGE.encode(), "text/html; charset=utf-8")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path.startswith("/device"):
                    set_device(body.get("device"))
                elif self.path.startswith("/vts"):
                    set_vts(body.get("host", ""))
            except (ValueError, TypeError):
                pass
            self._send(b"ok", "text/plain")

    return H


def main():
    from http.server import ThreadingHTTPServer

    ap = argparse.ArgumentParser(description="voice mood tray + overlay")
    ap.add_argument("--port", type=int, default=5095)
    ap.add_argument("--host", default="0.0.0.0",
                    help="bind address; 0.0.0.0 so a second PC can poll /status")
    args = ap.parse_args()

    load_settings()

    smile = opensmile.Smile(
        feature_set=opensmile.FeatureSet.eGeMAPSv02,
        feature_level=opensmile.FeatureLevel.Functionals,
    )

    srv = ThreadingHTTPServer((args.host, args.port), build_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"voice-mood serving http://127.0.0.1:{args.port}/", flush=True)

    q = queue.Queue()

    def cb(indata, frames, t_info, flag):
        q.put(indata[:, 0].copy())

    def drain():
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                return

    def capture_loop():
        chunk_n = int(SR * CHUNK_SECONDS)
        fast_n = max(int(SR * FAST_SECONDS), 1)
        while True:
            REOPEN.clear()
            drain()
            fast_buf = np.zeros(0, dtype=np.float32)
            buf = np.zeros(0, dtype=np.float32)
            cal = []
            baseline = None
            mouth = 0.0
            try:
                with sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                    blocksize=BLOCK, callback=cb,
                                    device=DEV["id"]):
                    while not REOPEN.is_set():
                        blk = q.get()
                        fast_buf = np.concatenate([fast_buf, blk])
                        buf = np.concatenate([buf, blk])
                        # fast path: level + mouth openness ~10x/sec
                        while len(fast_buf) >= fast_n:
                            piece, fast_buf = fast_buf[:fast_n], fast_buf[fast_n:]
                            db = min(0.0, 20.0 *
                                     np.log10(max(rms(piece), 1e-6)))
                            mouth += MOUTH_ALPHA * (mouth_open(db) - mouth)
                            with LOCK:
                                STATE.update(
                                    db=db, mouth=round(mouth, 3),
                                    ts=time.strftime("%H:%M:%S"))
                        # slow path: mood classification every 2s chunk
                        if len(buf) < chunk_n:
                            continue
                        piece, buf = buf[:chunk_n], buf[chunk_n:]
                        loud, pitch = extract(smile, piece)
                        if baseline is None:
                            cal.append(loud)
                            if len(cal) * CHUNK_SECONDS >= CALIBRATE_SECONDS:
                                baseline = float(np.median(cal))
                            continue
                        spread = pitch_spread(piece)
                        vratio = voiced_ratio(piece)
                        ts = time.strftime("%H:%M:%S")
                        stable_update(loud - baseline, pitch, spread, vratio,
                                      0.0, ts)
                        with LOCK:
                            mood = STATE["mood"]
                        color = COLORS.get(mood, "#9aa5b5")
                        try:
                            icon.icon = icon_image(color)
                            icon.title = f"{mood} ({ts})"
                        except Exception:
                            pass
                        print(f"[{ts}] mood: {mood:<12} f0={pitch:.1f}st "
                              f"spread={spread:.1f}st voiced={vratio:.0%} "
                              f"(device={DEV['id']})", flush=True)
            except Exception as e:  # bad / unplugged device - reopen with new one
                with LOCK:
                    STATE.update(mood=f"no input ({e})",
                                 ts=time.strftime("%H:%M:%S"))
                REOPEN.wait(1.0)

    def mic_menu(icon=None):
        items = [pystray.MenuItem("System default", lambda: set_device(None),
                                  checked=lambda i: DEV["id"] is None)]
        for d in input_devices():
            items.append(pystray.MenuItem(
                d["name"], lambda s, i=d["index"]: set_device(i),
                checked=lambda s, i=d["index"]: DEV["id"] == i))
        return tuple(items)

    icon = pystray.Icon(
        "voice-mood", icon_image("#9aa5b5"), "voice-mood starting",
        menu=pystray.Menu(
            pystray.MenuItem("Open page",
                             lambda: __import__("webbrowser").open(
                                 f"http://127.0.0.1:{args.port}/")),
            pystray.MenuItem("Demo cycle",
                             lambda: __import__("webbrowser").open(
                                 f"http://127.0.0.1:{args.port}/demo")),
            pystray.MenuItem("Microphone", pystray.Menu(mic_menu)),
            pystray.MenuItem("Quit", lambda: icon.stop())))

    threading.Thread(target=capture_loop, daemon=True).start()
    threading.Thread(target=vts_bridge, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
