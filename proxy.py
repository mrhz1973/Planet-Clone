"""
Proxy locale per i tile raster nautici di Navionics (piattaforma Garmin)
CON AUTO-REFRESH dei token: non devi piu' copiare nulla a mano.

Schema reale, verificato dal traffico di https://maps.garmin.com :

  1) Token (NESSUNA autenticazione richiesta):
       GET https://maps.garmin.com/marine/api/getNavionicsTokens
       -> { "access_token": <Bearer, scade ~2h>, "configuration_token": <config> }

  2) Tile:
       GET https://tile{1-5}.navionics.com/viewer/api/v1/tile/{z}/{x}/{y}
           ?config=<configuration_token>&transparent=false&ugc=false
           &layer=0&du=1&sd=2&sa=false
       Header: Authorization: Bearer <access_token>
               Origin / Referer: https://maps.garmin.com

Il proxy recupera i token all'avvio e li rinnova da solo prima della scadenza
(o subito, se un tile risponde 401). Aggira anche il CORS per Cesium.

Espone DUE carte (stesso flusso token, cambia solo "layer"):
  - /tiles/{z}/{x}/{y}.png  -> Seachart  (carta nautica, layer=0, opaca)
  - /sonar/{z}/{x}/{y}.png  -> SonarChart (batimetria, layer=1, trasparente:
                                si sovrappone alla nautica)

Avvio:  python proxy.py
Carta:  http://localhost:5000/tiles/{z}/{x}/{y}.png
Sonar:  http://localhost:5000/sonar/{z}/{x}/{y}.png
Stato:  http://localhost:5000/status
"""

import base64
import io
import json
import os
import random
import threading
import time

import requests
from flask import Flask, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# Parametri carta (stessi nomi dello script SAS.Planet):
#   layer:       0 = Seachart (nautica), 1 = SonarChart
#   transparent: false = strato base opaco, true = overlay sovrapponibile
#   ugc:         true = mostra i marker Active Captain (community Garmin), false = no
#   du:          unita' profondita' -> 1 metri, 2 piedi, 3 braccia
#   sd:          safe depth (profondita' di sicurezza)
#
# Parametri condivisi (configurabili via variabili d'ambiente):
UGC = os.environ.get("NAV_UGC", "false")
DU = os.environ.get("NAV_DU", "1")
SD = os.environ.get("NAV_SD", "2")

# Profili carta serviti dal proxy. Ogni route usa il proprio layer/transparent;
# il flusso token (config + Bearer) e' identico per entrambi.
#   Seachart   -> base opaca (layer 0)
#   SonarChart -> overlay trasparente (layer 1), da sovrapporre alla Seachart
SEACHART_PARAMS = {
    "transparent": "false", "ugc": UGC, "layer": "0", "du": DU, "sd": SD, "sa": "false",
}
SONARCHART_PARAMS = {
    "transparent": "true", "ugc": UGC, "layer": "1", "du": DU, "sd": SD, "sa": "false",
}

TOKENS_URL = "https://maps.garmin.com/marine/api/getNavionicsTokens"
TILE_HOST = "https://tile{n}.navionics.com"
TILE_PATH = "/viewer/api/v1/tile/{z}/{x}/{y}"
ORIGIN = "https://maps.garmin.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
)
# Rinnova quando all'access_token restano meno di questi secondi.
REFRESH_MARGIN = 120

TRANSPARENT_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

_session = requests.Session()
_lock = threading.Lock()
_state = {"access": None, "config": None, "exp": 0.0, "error": None}


def _base_headers():
    return {"User-Agent": USER_AGENT, "Origin": ORIGIN, "Referer": ORIGIN + "/", "Accept": "*/*"}


def _jwt_exp(token):
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return float(json.loads(base64.urlsafe_b64decode(p))["exp"])
    except Exception:
        return 0.0


def _refresh_tokens():
    """Scarica una nuova coppia di token da Garmin (nessuna auth necessaria)."""
    try:
        r = _session.get(TOKENS_URL, headers=_base_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        access = data["access_token"]
        config = data["configuration_token"]
        _state.update(access=access, config=config, exp=_jwt_exp(access), error=None)
        left = (_state["exp"] - time.time()) / 60
        print(f"[proxy] Token aggiornati (access valido ~{left:.0f} min).")
        return True
    except Exception as exc:
        _state["error"] = str(exc)
        print(f"[proxy] Errore aggiornamento token: {exc}")
        return False


def _ensure_tokens(force=False):
    """Restituisce (access, config) validi, rinnovando se serve. Thread-safe."""
    with _lock:
        near_expiry = (_state["exp"] - time.time()) < REFRESH_MARGIN
        if force or _state["access"] is None or near_expiry:
            _refresh_tokens()
        return _state["access"], _state["config"]


@app.route("/status")
def status():
    access, config = _ensure_tokens()
    left = (_state["exp"] - time.time()) / 60 if _state["exp"] else None
    return jsonify({
        "tokens_ok": bool(access and config),
        "access_minutes_left": round(left, 1) if left is not None else None,
        "charts": {
            "seachart": "/tiles/{z}/{x}/{y}.png",
            "sonarchart": "/sonar/{z}/{x}/{y}.png",
        },
        "last_error": _state["error"],
    })


def _serve_tile(z, x, y, chart_params, label):
    """Scarica un tassello Navionics con il profilo carta richiesto e lo serve.

    Su token scaduto (401) rinnova e riprova una volta. Su errore/tassello
    mancante restituisce un PNG trasparente, cosi' Cesium non logga errori.
    """
    for attempt in range(2):
        access, config = _ensure_tokens(force=(attempt == 1))
        if not access or not config:
            break

        url = TILE_HOST.format(n=random.randint(1, 5)) + TILE_PATH.format(z=z, x=x, y=y)
        headers = dict(_base_headers(), Authorization=f"Bearer {access}")
        params = dict(chart_params, config=config)
        try:
            resp = _session.get(url, headers=headers, params=params, timeout=10)
        except requests.RequestException as exc:
            print(f"[proxy] Errore rete {label} {z}/{x}/{y}: {exc}")
            break

        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and resp.content and "image" in ctype:
            return send_file(io.BytesIO(resp.content), mimetype="image/png")

        # 401 = access_token scaduto/non valido -> rinnova e riprova una volta
        if resp.status_code == 401 and attempt == 0:
            print(f"[proxy] 401 su {label} {z}/{x}/{y}: rinnovo token...")
            continue

        print(f"[proxy] {label} {z}/{x}/{y} -> {resp.status_code} ({ctype})")
        break

    return send_file(io.BytesIO(TRANSPARENT_PNG), mimetype="image/png")


@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def proxy_seachart(z, x, y):
    return _serve_tile(z, x, y, SEACHART_PARAMS, "seachart")


@app.route("/sonar/<int:z>/<int:x>/<int:y>.png")
def proxy_sonarchart(z, x, y):
    return _serve_tile(z, x, y, SONARCHART_PARAMS, "sonar")


if __name__ == "__main__":
    print("Proxy Navionics (Garmin) con auto-refresh su http://localhost:5000")
    print("Carta: http://localhost:5000/tiles/{z}/{x}/{y}.png  (Seachart)")
    print("Sonar: http://localhost:5000/sonar/{z}/{x}/{y}.png  (SonarChart)")
    print("Stato: http://localhost:5000/status")
    if _ensure_tokens()[0]:
        print("Token iniziali ottenuti automaticamente. Tutto pronto.\n")
    else:
        print("\n[ATTENZIONE] Impossibile ottenere i token iniziali da Garmin.")
        print("  Controlla la connessione o l'endpoint getNavionicsTokens.\n")
    app.run(port=5000, debug=False, threaded=True)
