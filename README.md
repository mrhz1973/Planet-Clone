# CesiumTest — Carte nautiche Navionics in CesiumJS

Visualizzatore 3D basato su [CesiumJS](https://cesium.com/platform/cesiumjs/) che
mostra le **carte nautiche Navionics** (piattaforma Garmin) tramite un piccolo
**proxy Python** locale.

Il proxy risolve tre problemi:

- **CORS** — il browser parla solo con `localhost`, il proxy scarica i tasselli da server a server.
- **Autenticazione** — recupera e rinnova automaticamente i token Navionics/Garmin.
- **Token a scadenza** — l'`access_token` dura ~2h e viene rigenerato da solo.

## Come funziona

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

Il proxy (`proxy.py`) espone i tasselli su
`http://localhost:5000/tiles/{z}/{x}/{y}.png` e Cesium li carica come
`UrlTemplateImageryProvider`.

## Requisiti

- Python 3.x
- Node (solo per scaricare il pacchetto `cesium` via npm)

## Installazione

```bash
# Dipendenze Python
python -m pip install -r requirements.txt

# Dipendenze frontend (scarica CesiumJS in node_modules/)
npm install
```

## Avvio

Servono due terminali nella cartella del progetto.

**Terminale 1 — proxy Navionics:**

```bash
python proxy.py
```

**Terminale 2 — server web statico:**

```bash
python -m http.server 8000
```

Poi apri **http://localhost:8000/index.html** (sempre via `http://`, non con
doppio clic sul file).

Verifica lo stato dei token: **http://localhost:5000/status**

## Configurazione (variabili d'ambiente, opzionali)

| Variabile | Default | Significato |
|---|---|---|
| `NAV_LAYER` | `0` | 0 = carta nautica, 1 = SonarChart |
| `NAV_UGC` | `false` | `true` = marker Active Captain (community Garmin) |
| `NAV_TRANSPARENT` | `false` | `true` = overlay trasparente |
| `NAV_DU` | `1` | profondità: 1 metri, 2 piedi, 3 braccia |
| `NAV_SD` | `2` | safe depth |

Esempio (PowerShell):

```powershell
$env:NAV_LAYER="1"; $env:NAV_UGC="true"; python proxy.py
```

## Avvertenze

- Dipende dal flusso web attuale di Garmin/Navionics: se cambiano endpoint o
  formato dei token, `proxy.py` andrà aggiornato.
- Uso personale/didattico. Rispetta i Termini di Servizio di Navionics/Garmin.

## File principali

- `proxy.py` — proxy Flask con auto-refresh dei token.
- `index.html` — visualizzatore CesiumJS che punta al proxy.
- `requirements.txt` — dipendenze Python.
- `package.json` — dipendenza CesiumJS.
