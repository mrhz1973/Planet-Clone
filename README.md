# 🌊 Planet-Clone — Carte nautiche Navionics in CesiumJS

![CesiumJS](https://img.shields.io/badge/CesiumJS-1.142-48b)
![Python](https://img.shields.io/badge/Python-3.x-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-proxy-000000?logo=flask&logoColor=white)
![Charts](https://img.shields.io/badge/charts-Navionics%20(Garmin)-1e90ff)

Visualizzatore 3D basato su [CesiumJS](https://cesium.com/platform/cesiumjs/) che
mostra le **carte nautiche Navionics** (piattaforma Garmin) dentro un globo
terrestre, tramite un piccolo **proxy Python** locale.

> ℹ️ Per il contesto tecnico completo, la storia del reverse-engineering e i
> vicoli ciechi già esplorati, vedi **[`AGENTS.md`](./AGENTS.md)** (pensato anche
> per essere letto da assistenti AI come Cursor).

---

## ⚡ TL;DR — avvio in 30 secondi

```bash
python -m pip install -r requirements.txt   # backend
npm install                                 # scarica CesiumJS

# poi, in due terminali separati:
python proxy.py                             # terminale 1  -> porta 5000
python -m http.server 8000                  # terminale 2  -> porta 8000
```

Apri **http://localhost:8000/index.html** 🎉

---

## 🧩 Perché serve un proxy

Le carte Navionics non si possono caricare direttamente nel browser perché:

| Problema | Soluzione del proxy |
|---|---|
| **CORS** — il browser non può chiamare i server Navionics | Il browser parla solo con `localhost`; il proxy scarica i tasselli server-to-server |
| **Autenticazione** — i tasselli richiedono un token | Il proxy ottiene i token da Garmin e li allega alle richieste |
| **Token a scadenza** (~2h) | Il proxy li **rinnova automaticamente** leggendo la scadenza dal JWT |

## 🏗️ Architettura

```
Browser (CesiumJS, index.html)
   │  GET http://localhost:5000/tiles/{z}/{x}/{y}.png
   ▼
proxy.py  (Flask, porta 5000)
   │  1) ottiene/rinnova i token da Garmin
   │  2) scarica il tassello da Navionics con Authorization: Bearer
   ▼
maps.garmin.com  +  tile{1-5}.navionics.com
```

Lo schema replica quello del visualizzatore ufficiale `maps.garmin.com`:

1. **Token** (nessuna autenticazione richiesta):

   ```
   GET https://maps.garmin.com/marine/api/getNavionicsTokens
   -> { "access_token": <Bearer, ~2h>, "configuration_token": <config> }
   ```

2. **Tasselli**:

   ```
   GET https://tile{1-5}.navionics.com/viewer/api/v1/tile/{z}/{x}/{y}
       ?config=<configuration_token>&transparent=false&ugc=false&layer=0&du=1&sd=2&sa=false
   Header: Authorization: Bearer <access_token>
           Origin / Referer: https://maps.garmin.com
   ```

## 📦 Requisiti

- **Python 3.x**
- **Node.js / npm** (solo per scaricare il pacchetto `cesium`)

## 🚀 Installazione e avvio

```bash
git clone https://github.com/mrhz1973/Planet-Clone.git
cd Planet-Clone

python -m pip install -r requirements.txt
npm install
```

Servono **due terminali** nella cartella del progetto:

**Terminale 1 — proxy Navionics:**

```bash
python proxy.py
```

**Terminale 2 — server web statico:**

```bash
python -m http.server 8000
```

Poi apri **http://localhost:8000/index.html** (sempre via `http://`, **mai** con
doppio clic sul file `file://`, altrimenti Cesium e le chiamate al proxy falliscono).

Diagnostica token: **http://localhost:5000/status** → deve mostrare `"tokens_ok": true`.

Overlay raster aggiuntivi (pass-through allowlisted, senza token Navionics):

- Strava Heatmap Run (maxZoom 11): **http://localhost:5000/strava-run/{z}/{x}/{y}.png**
- Hillshade OSM US (maxZoom 12): **http://localhost:5000/hillshade/{z}/{x}/{y}.jpg**

## ⚙️ Configurazione (variabili d'ambiente, opzionali)

| Variabile | Default | Significato |
|---|---|---|
| `NAV_LAYER` | `0` | `0` = carta nautica, `1` = SonarChart |
| `NAV_UGC` | `false` | `true` = marker Active Captain (community Garmin) |
| `NAV_TRANSPARENT` | `false` | `true` = overlay trasparente |
| `NAV_DU` | `1` | profondità: `1` metri, `2` piedi, `3` braccia |
| `NAV_SD` | `2` | safe depth (profondità di sicurezza) |

Esempio (PowerShell) — SonarChart con marker community:

```powershell
$env:NAV_LAYER="1"; $env:NAV_UGC="true"; python proxy.py
```

## 🛠️ Risoluzione problemi

| Sintomo | Causa probabile | Rimedio |
|---|---|---|
| Globo nero, nessuna carta | Proxy non avviato o token KO | Apri `http://localhost:5000/status`; riavvia `python proxy.py` |
| Tasselli trasparenti dopo un po' | `access_token` scaduto | Il proxy si auto-rinnova; se persiste, riavvia il proxy |
| Errori CORS in console | Pagina aperta come `file://` | Apri sempre via `http://localhost:8000/index.html` |
| `python: can't open file ... proxy.py` | Sei nella cartella sbagliata | `cd` nella cartella del progetto prima di avviare |
| Carta solo sul mare, terraferma nera | Nessuna mappa di base (scelta voluta) | Vedi `index.html` per aggiungere uno strato di base |

## 📁 Struttura del progetto

| File | Ruolo |
|---|---|
| `proxy.py` | Proxy Flask con auto-refresh dei token Garmin/Navionics |
| `index.html` | Visualizzatore CesiumJS (solo strato Navionics, globo nero sotto) |
| `requirements.txt` | Dipendenze Python: flask, flask-cors, requests |
| `package.json` | Dipendenza frontend: `cesium` |
| `.gitignore` | Esclude `node_modules/`, cache Python, file temporanei |
| `README.md` | Questa guida |
| `AGENTS.md` | Contesto tecnico completo e storia del progetto |

> `node_modules/` (CesiumJS) **non è versionato**: si ricrea con `npm install`.

## ⚠️ Avvertenze

- L'intera catena dipende dal **flusso web attuale di Garmin/Navionics**: se
  cambiano endpoint o formato dei token, `proxy.py` andrà aggiornato.
- Progetto a **uso personale/didattico**. Rispetta i Termini di Servizio di
  Navionics/Garmin.
