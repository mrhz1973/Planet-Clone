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

Espone DUE carte Navionics (stesso flusso token, cambia solo "layer"):
  - /tiles/{z}/{x}/{y}.png  -> Seachart  (carta nautica, layer=0, opaca)
  - /sonar/{z}/{x}/{y}.png  -> SonarChart (batimetria, layer=1, trasparente:
                                si sovrappone alla nautica)

Espone inoltre Google Satellite (handler separato, NO token Navionics):
  - /gsat/{z}/{x}/{y}.jpg   -> Google Satellite (version discovery + fallback mt)

Espone Bing Satellite (handler separato, NO token Navionics):
  - /bsat/{z}/{x}/{y}.jpg   -> Bing Satellite (quadkey + version discovery / env)

Avvio:  python proxy.py
Carta:  http://localhost:5000/tiles/{z}/{x}/{y}.png
Sonar:  http://localhost:5000/sonar/{z}/{x}/{y}.png
G.Sat:  http://localhost:5000/gsat/{z}/{x}/{y}.jpg
B.Sat:  http://localhost:5000/bsat/{z}/{x}/{y}.jpg
Stato:  http://localhost:5000/status
"""

import base64
import io
import json
import os
import random
import re
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

# ---------------------------------------------------------------------------
# Google Satellite (WU-0009) — handler separato dal token-flow Navionics.
# Cache slot / id "gsat" (NON "sat": collide con Esri sat nel monolite GIS).
# z dalla route = XYZ standard 0-based del client: NON applicare offset SAS z-1.
# ---------------------------------------------------------------------------
GSAT_CHART_ID = "gsat"
GOOGLE_MAPS_JS_URL = "https://maps.googleapis.com/maps/api/js"
_GSAT_VERSION_RE = re.compile(r"https://khms\d+\.googleapis\.com/kh\?v=(\d+)")
GSAT_STATIC_VERSION = (os.environ.get("GSAT_STATIC_VERSION") or "").strip() or None
GSAT_LANG = (os.environ.get("GSAT_LANG") or "en").strip() or "en"
GSAT_VERSION_TTL = int(os.environ.get("GSAT_VERSION_TTL", "3600"))
GOOGLE_REFERER = "https://maps.google.com/"

_gsat_lock = threading.Lock()
_gsat_version_state = {
    "version": None,
    "source": None,
    "fetched_at": 0.0,
    "error": None,
}


def _gsat_discovery_headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": GOOGLE_REFERER,
    }


def _gsat_tile_headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": GOOGLE_REFERER,
    }


def _pick_gsat_subdomain():
    return random.randint(0, 3)


def _discover_gsat_version_unlocked():
    """Regex su maps/api/js; chiamare solo con _gsat_lock."""
    try:
        resp = _session.get(
            GOOGLE_MAPS_JS_URL,
            headers=_gsat_discovery_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        match = _GSAT_VERSION_RE.search(resp.text)
        if match:
            ver = match.group(1)
            _gsat_version_state.update(
                version=ver, source="discovery", fetched_at=time.time(), error=None,
            )
            print(f"[proxy] Google Satellite version discovered: v={ver}")
            return ver
        _gsat_version_state["error"] = "version regex no match in maps/api/js"
        print("[proxy] Google Satellite: version regex no match")
    except Exception as exc:
        _gsat_version_state["error"] = str(exc)
        print(f"[proxy] Google Satellite version discovery failed: {exc}")
    return None


def _ensure_gsat_version(force=False):
    """Version cache process-level con lock; fallback statico da env."""
    with _gsat_lock:
        cached = _gsat_version_state.get("version")
        age = time.time() - float(_gsat_version_state.get("fetched_at") or 0)
        if not force and cached and age < GSAT_VERSION_TTL:
            return cached, _gsat_version_state.get("source") or "discovery"

        ver = _discover_gsat_version_unlocked()
        if ver:
            return ver, "discovery"

        if GSAT_STATIC_VERSION:
            _gsat_version_state.update(
                version=GSAT_STATIC_VERSION,
                source="static",
                fetched_at=time.time(),
                error=None,
            )
            print(f"[proxy] Google Satellite using static version v={GSAT_STATIC_VERSION}")
            return GSAT_STATIC_VERSION, "static"

        _gsat_version_state.update(version=None, source=None, fetched_at=time.time())
        return None, None


def _fetch_gsat_khms(z, x, y, version):
    """z passato cosi' com'e' (0-based XYZ); nessun offset SAS."""
    s = _pick_gsat_subdomain()
    url = f"https://khms{s}.google.com/kh/v={version}&src=app&x={x}&y={y}&z={z}"
    resp = _session.get(url, headers=_gsat_tile_headers(), timeout=15)
    ctype = resp.headers.get("content-type", "")
    if resp.status_code == 200 and resp.content and "image" in ctype:
        return resp.content
    print(f"[proxy] gsat khms {z}/{x}/{y} -> {resp.status_code} ({ctype})")
    return None


def _fetch_gsat_mt(z, x, y):
    """Fallback senza versione (lyrs=s)."""
    s = _pick_gsat_subdomain()
    url = f"https://mt{s}.google.com/vt/lyrs=s&hl={GSAT_LANG}&x={x}&y={y}&z={z}"
    try:
        resp = _session.get(url, headers=_gsat_tile_headers(), timeout=15)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and resp.content and "image" in ctype:
            return resp.content
        print(f"[proxy] gsat mt {z}/{x}/{y} -> {resp.status_code} ({ctype})")
    except requests.RequestException as exc:
        print(f"[proxy] gsat mt {z}/{x}/{y}: {exc}")
    return None


def _fetch_gsat_tile_bytes(z, x, y):
    """Ordine: khms versionato (con refresh su miss) -> mt senza versione."""
    for attempt in range(2):
        version, _src = _ensure_gsat_version(force=(attempt == 1))
        if version:
            try:
                data = _fetch_gsat_khms(z, x, y, version)
                if data:
                    return data, "khms"
            except requests.RequestException as exc:
                print(f"[proxy] gsat khms {z}/{x}/{y}: {exc}")
            if attempt == 0:
                continue
        break

    data = _fetch_gsat_mt(z, x, y)
    if data:
        return data, "mt"
    return None, None


def _gsat_status_payload():
    version, source = _ensure_gsat_version()
    age = time.time() - float(_gsat_version_state.get("fetched_at") or 0)
    return {
        "route": "/gsat/{z}/{x}/{y}.jpg",
        "cache_dir_name": GSAT_CHART_ID,
        "version": version,
        "version_source": source,
        "version_age_sec": round(age, 1) if _gsat_version_state.get("fetched_at") else None,
        "static_fallback_configured": bool(GSAT_STATIC_VERSION),
        "discovery_host": "maps.googleapis.com",
        "tile_hosts": ["khms*.google.com", "mt*.google.com"],
        "last_error": _gsat_version_state.get("error"),
    }


# ---------------------------------------------------------------------------
# Bing Satellite (WU-0009B) — handler separato da Navionics e Google Satellite.
# cache_dir_name / chart id "bsat" e' solo etichetta (pass-through, no cache disco).
# z dalla route = XYZ standard 0-based: quadkey Bing canonico, nessun offset SAS z-1.
# ---------------------------------------------------------------------------
BSAT_CHART_ID = "bsat"
BING_MAPS_URL = "https://www.bing.com/maps"
_BSAT_VERSION_RE = re.compile(
    r"https?://[^\"'\s]*(?:ssl\.ak\.)?tiles\.virtualearth\.net[^\"'\s]*[?&]g=(\d+)",
    re.IGNORECASE,
)
BING_STATIC_VERSION = (os.environ.get("BING_STATIC_VERSION") or "").strip() or None
BING_VERSION_TTL = int(os.environ.get("BING_VERSION_TTL", "3600"))
BING_REFERER = "https://www.bing.com/"

_bsat_lock = threading.Lock()
_bsat_version_state = {
    "version": None,
    "source": None,
    "fetched_at": 0.0,
    "error": None,
}


def _bing_quadkey(z, x, y):
    """Quadkey Bing canonico: z passato cosi' com'e', bit x/y per livello da z a 1."""
    quadkey = []
    for level in range(z, 0, -1):
        digit = 0
        mask = 1 << (level - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        quadkey.append(str(digit))
    return "".join(quadkey)


def _pick_bsat_subdomain(quadkey):
    return len(quadkey) % 8


def _bsat_discovery_headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": BING_REFERER,
    }


def _bsat_tile_headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept": "image/webp,image/apng,image/jpeg,image/*,*/*;q=0.8",
        "Referer": BING_REFERER,
    }


def _discover_bsat_version_unlocked():
    """Regex ancorata a URL tile Virtual Earth; chiamare solo con _bsat_lock."""
    try:
        resp = _session.get(
            BING_MAPS_URL,
            headers=_bsat_discovery_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        match = _BSAT_VERSION_RE.search(resp.text)
        if match:
            ver = match.group(1)
            _bsat_version_state.update(
                version=ver, source="discovery", fetched_at=time.time(), error=None,
            )
            print(f"[proxy] Bing Satellite version discovered: g={ver}")
            return ver
        _bsat_version_state["error"] = "version regex no match in bing.com/maps"
        print("[proxy] Bing Satellite: version regex no match")
    except Exception as exc:
        _bsat_version_state["error"] = str(exc)
        print(f"[proxy] Bing Satellite version discovery failed: {exc}")
    return None


def _ensure_bsat_version(force=False):
    """Version cache process-level con lock; fallback statico da env."""
    with _bsat_lock:
        cached = _bsat_version_state.get("version")
        age = time.time() - float(_bsat_version_state.get("fetched_at") or 0)
        if not force and cached and age < BING_VERSION_TTL:
            return cached, _bsat_version_state.get("source") or "discovery"

        ver = _discover_bsat_version_unlocked()
        if ver:
            return ver, "discovery"

        if BING_STATIC_VERSION:
            _bsat_version_state.update(
                version=BING_STATIC_VERSION,
                source="static",
                fetched_at=time.time(),
                error=None,
            )
            print(f"[proxy] Bing Satellite using static version g={BING_STATIC_VERSION}")
            return BING_STATIC_VERSION, "static"

        _bsat_version_state.update(version=None, source=None, fetched_at=time.time())
        return None, None


def _fetch_bsat_tile_bytes(z, x, y):
    """Pass-through Bing tile: quadkey canonico + versione discovery/env."""
    version, _src = _ensure_bsat_version()
    if not version:
        print(f"[proxy] bsat {z}/{x}/{y}: no Bing version (discovery failed, no env)")
        return None

    quadkey = _bing_quadkey(z, x, y)
    s = _pick_bsat_subdomain(quadkey)
    url = (
        f"https://t{s}.ssl.ak.tiles.virtualearth.net/tiles/a{quadkey}.jpeg"
        f"?g={version}"
    )
    try:
        resp = _session.get(url, headers=_bsat_tile_headers(), timeout=15)
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and resp.content and "image" in ctype:
            return resp.content
        print(f"[proxy] bsat {z}/{x}/{y} -> {resp.status_code} ({ctype})")
    except requests.RequestException as exc:
        print(f"[proxy] bsat {z}/{x}/{y}: {exc}")
    return None


def _bsat_status_payload():
    version, source = _ensure_bsat_version()
    age = time.time() - float(_bsat_version_state.get("fetched_at") or 0)
    return {
        "route": "/bsat/{z}/{x}/{y}.jpg",
        "cache_dir_name": BSAT_CHART_ID,
        "version": version,
        "version_source": source,
        "version_age_sec": round(age, 1) if _bsat_version_state.get("fetched_at") else None,
        "static_fallback_configured": bool(BING_STATIC_VERSION),
        "discovery_host": "www.bing.com",
        "tile_hosts": ["*.ssl.ak.tiles.virtualearth.net"],
        "last_error": _bsat_version_state.get("error"),
    }


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
            "gsat": "/gsat/{z}/{x}/{y}.jpg",
            "bsat": "/bsat/{z}/{x}/{y}.jpg",
        },
        "gsat": _gsat_status_payload(),
        "bsat": _bsat_status_payload(),
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


@app.route("/gsat/<int:z>/<int:x>/<int:y>.jpg")
def proxy_gsat(z, x, y):
    """Google Satellite — z XYZ 0-based dal client, senza offset SAS z-1."""
    data, via = _fetch_gsat_tile_bytes(z, x, y)
    if data:
        return send_file(io.BytesIO(data), mimetype="image/jpeg")
    return jsonify({"error": "gsat tile unavailable", "z": z, "x": x, "y": y}), 502


@app.route("/bsat/<int:z>/<int:x>/<int:y>.jpg")
def proxy_bsat(z, x, y):
    """Bing Satellite — z XYZ 0-based dal client, quadkey canonico senza offset SAS."""
    data = _fetch_bsat_tile_bytes(z, x, y)
    if data:
        return send_file(io.BytesIO(data), mimetype="image/jpeg")
    return jsonify({"error": "bsat tile unavailable", "z": z, "x": x, "y": y}), 502


if __name__ == "__main__":
    print("Proxy Navionics (Garmin) con auto-refresh su http://localhost:5000")
    print("Carta: http://localhost:5000/tiles/{z}/{x}/{y}.png  (Seachart)")
    print("Sonar: http://localhost:5000/sonar/{z}/{x}/{y}.png  (SonarChart)")
    print("G.Sat: http://localhost:5000/gsat/{z}/{x}/{y}.jpg  (Google Satellite)")
    print("B.Sat: http://localhost:5000/bsat/{z}/{x}/{y}.jpg  (Bing Satellite)")
    print("Stato: http://localhost:5000/status")
    if _ensure_tokens()[0]:
        print("Token iniziali ottenuti automaticamente. Tutto pronto.\n")
    else:
        print("\n[ATTENZIONE] Impossibile ottenere i token iniziali da Garmin.")
        print("  Controlla la connessione o l'endpoint getNavionicsTokens.\n")
    ver, src = _ensure_gsat_version()
    if ver:
        print(f"Google Satellite version pronta: v={ver} (source={src})\n")
    else:
        print("[INFO] Google Satellite: nessuna versione khms; i tile useranno fallback mt.\n")
    bver, bsrc = _ensure_bsat_version()
    if bver:
        print(f"Bing Satellite version pronta: g={bver} (source={bsrc})\n")
    else:
        print("[INFO] Bing Satellite: nessuna versione (discovery fallita, env assente).\n")
    app.run(port=5000, debug=False, threaded=True)
