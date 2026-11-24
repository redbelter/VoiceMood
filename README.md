# VoiceMood

Lightweight real-time voice mood analyzer for Windows. Reads your microphone,
extracts prosody with openSMILE (eGeMAPSv02), classifies your mood against
your *own* speaking baseline, and shows the result on a transparent overlay
page designed for OBS Browser Sources. A tray icon reflects the current mood.

## What it does

- Captures mic audio in 2-second chunks (~10 ms of CPU per chunk)
- Extracts loudness (dBFS), pitch (F0), intonation spread, voiced ratio
- Z-scores everything against a slowly-drifting per-speaker baseline, so it
  adapts to *your* normal voice instead of fixed Hz/dB thresholds
- Commits label changes only after 2 consecutive agreeing chunks (no flicker)
- Holds readings through natural pauses; sustained silence shows `idle`
  (useful for spotting a muted or unplugged mic on stream)

### Mood labels

| Label | Prosody signature | Color |
|---|---|---|
| excited | loud/fast + wide melody | amber |
| cheerful | bright pitch, up-tempo | gold |
| warm | soft, bright, easy pace | rose |
| focused | steady stream of words | blue |
| driven | high output, steady pitch | teal-green |
| calm | gentle, unhurried | teal |
| tired | quiet and sparse, below own norm | lavender |
| frustrated | loud/fast, clipped monotone | terracotta |
| neutral | baseline | grey |
| idle | sustained silence / muted mic | dark grey |

## Endpoints (default port 5095)

| URL | Contents |
|---|---|
| `http://127.0.0.1:5095/` | Overlay page + mic picker (top-left dropdown) |
| `http://127.0.0.1:5095/?bare` | Overlay only, no settings panel — use this in OBS |
| `http://127.0.0.1:5095/demo` | Scripted cycle through all mood looks, no mic needed |
| `http://127.0.0.1:5095/status` | JSON state for custom bridges |
| `POST /device {"device": N}` | Switch input device (persisted to settings.json) |

### Example `/status` payload

```json
{"mood":"focused","f0":21.9,"spread":7.4,"loud":4.1,"loud_rel":1.2,
 "voiced":0.61,"db":-23.4,"mouth":0.67,"ts":"16:21:45"}
```

`mouth` is a smoothed 0–1 lip-openness value derived from input dBFS —
poll `/status` from a VTube Studio Output Face Tracker (or any overlay
bridge) and feed it straight into the mouth Openness parameter. Loud speech
opens the mouth wider; silence closes it.

## OBS setup

Add a Browser Source:

- URL: `http://127.0.0.1:5095/?bare`
- Size: 1920 × 1080, transform at 100%
- The page positions its own card bottom-right inside the safe zone, with a
  transparent surround; no chroma-key filter needed
- Leave "Shutdown source when not visible" off

## VTube Studio bridge

Built into the app — no separate process. On the overlay page (`http://127.0.0.1:5095/`)
type the VTube Studio machine's IP into the box next to the mic dropdown
(e.g. `192.168.15.16`); the app then keeps `MouthOpen` injected into the VTS
Plugin API (~12 Hz) automatically. VTS side: toggle **"Allow Plugin API
access"** on its main config page (default port 8001). First connect asks VTS
to approve the plugin once; the token is cached in `vts_token.json`. Clear the
box to disable the bridge. Level/mouth values refresh ~10x/sec, independent of
the 2-second mood window.

## Run

```powershell
.\.venv\Scripts\python.exe voice_mood.py            # tray icon + server
powershell -File .\_restart.ps1                     # kill stale listener, relaunch hidden
```

Dependencies (into `.venv`): `opensmile sounddevice numpy pystray pillow`.

## Tray icon

Ring color tracks the mood. Menu: Open page · Demo cycle · Microphone ▸ · Quit.
The mic dropdown on the web page and the tray submenu write the same
`settings.json`; no command-line flags needed for day-to-day use.
