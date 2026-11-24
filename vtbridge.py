"""Bridge: mic-PC /status -> VTube Studio MouthOpen over the Plugin API.

Run this on the machine that runs VTube Studio. It polls the voice-mood
status JSON (served by the machine with the mic) and injects the smoothed
mouth value into VTS at least twice per second, which is what the API
requires to keep control of a parameter.

    python vtbridge.py --status http://MICPC:5095/status

In VTube Studio: Settings -> "Allow Plugin API access" (port shown there,
default 8001). First run grabs an auth token automatically and caches it
in vts_token.json next to this script.
"""
import argparse
import json
import os
import time
import urllib.request

from websocket import create_connection

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "vts_token.json")
PLUGIN = {"pluginName": "VoiceMood", "pluginDeveloper": "redbelter"}


def _msg(mtype, data=None):
    m = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
         "requestID": "voicemood", "messageType": mtype}
    if data is not None:
        m["data"] = data
    return json.dumps(m)


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


def authenticate(ws, forced=None):
    token = forced or load_token()
    if not token:
        ws.send(_msg("AuthenticationTokenRequest", PLUGIN))
        token = json.loads(ws.recv())["data"]["authenticationToken"]
        save_token(token)
    data = dict(PLUGIN)
    data["authenticationToken"] = token
    ws.send(_msg("AuthenticationRequest", data))
    json.loads(ws.recv())


def fetch_status(url):
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.loads(r.read().decode("utf-8"))


def inject(ws, value, weight):
    ws.send(_msg("InjectParameterDataRequest", {
        "faceFound": False, "mode": "set",
        "parameterValues": [{"id": "MouthOpen", "value": value,
                             "weight": weight}]}))
    ws.recv()


def main():
    ap = argparse.ArgumentParser(description="VoiceMood -> VTube Studio mouth bridge")
    ap.add_argument("--status", default="http://127.0.0.1:5095/status",
                    help="voice_mood /status URL on the mic machine")
    ap.add_argument("--api-host", default="localhost")
    ap.add_argument("--api-port", type=int, default=8001)
    ap.add_argument("--gain", type=float, default=1.3,
                    help="multiplier on the 0..1 mouth value")
    ap.add_argument("--weight", type=float, default=1.0,
                    help="0..1 mix vs. webcam face tracking in VTS")
    ap.add_argument("--interval", type=float, default=0.08,
                    help="poll interval seconds (keep under 1.0)")
    args = ap.parse_args()

    ws = None
    while True:
        try:
            if ws is None:
                ws = create_connection(
                    f"ws://{args.api_host}:{args.api_port}", timeout=5)
                authenticate(ws)
            s = fetch_status(args.status)
            mouth = min(1.0, max(0.0, float(s.get("mouth", 0.0)) * args.gain))
            inject(ws, mouth, args.weight)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"reconnect: {e}", flush=True)
            try:
                ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(1)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
