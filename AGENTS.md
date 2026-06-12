# Contesto del progetto (per sviluppatori e agenti AI)

Questo file riassume **cos'è il progetto, com'è strutturato, cosa è stato
scoperto e perché le cose sono fatte così**. È pensato per essere letto da una
persona o da un assistente AI (Cursor, ecc.) che apre il repo per la prima volta
su un altro computer, in modo da avere subito il quadro completo.

---

## 1. Obiettivo

Mostrare le **carte nautiche Navionics** (oggi sulla piattaforma **Garmin**)
dentro un globo 3D **CesiumJS**, eseguibile in locale su Windows.

Il nodo del problema NON è Cesium: è **come ottenere i tasselli (tile) di
Navionics**, perché:

- Navionics blocca le richieste cross-origin dal browser (**CORS**).
- I tasselli richiedono **autenticazione** (un token a scadenza).
- Gli endpoint pubblici "storici" non funzionano più dopo il passaggio a Garmin.

La soluzione è un **proxy Python locale** (`proxy.py`) che si autentica come fa
il visualizzatore ufficiale `maps.garmin.com`, scarica i tasselli server-to-server
(niente CORS) e li serve a Cesium.

---

## 2. Architettura

```
Browser (CesiumJS, index.html)
   │  GET http://localhost:5000/tiles/{z}/{x}/{y}.png
   ▼
proxy.py  (Flask + flask-cors, porta 5000)
   │  1) ottiene/rinnova i token da Garmin
   │  2) scarica il tassello da Navionics con Authorization: Bearer
   ▼
maps.garmin.com  +  tile{1-5}.navionics.com
```

Servono **due processi locali** in esecuzione contemporaneamente:

1. `python proxy.py` → proxy tasselli su `http://localhost:5000`
2. `python -m http.server 8000` → serve `index.html` su `http://localhost:8000`

La pagina va aperta via **http://localhost:8000/index.html** (mai `file://`,
altrimenti Cesium e le chiamate al proxy falliscono).

---

## 3. Lo schema reale Navionics/Garmin (la scoperta chiave)

Questo è il cuore del progetto, ricavato analizzando il traffico di
`maps.garmin.com` e confrontandolo con lo script `GetUrlScript.txt` di SAS.Planet.

### Passo 1 — Ottenere i token (NESSUNA autenticazione richiesta)

```
GET https://maps.garmin.com/marine/api/getNavionicsTokens
Header:  Origin: https://maps.garmin.com
         Referer: https://maps.garmin.com/

Risposta JSON:
{
  "access_token":        "<JWT, aud=maps.garmin.com, scade ~2h>",
  "configuration_token": "<JWT con apr=codice carta, NON scade>"
}
```

Sorprendentemente l'endpoint **non richiede login né cookie**: chiunque può
ottenere i token. Questo permette l'auto-refresh.

### Passo 2 — Scaricare un tassello

```
GET https://tile{1-5}.navionics.com/viewer/api/v1/tile/{z}/{x}/{y}
    ?config=<configuration_token>
    &transparent=false&ugc=false&layer=0&du=1&sd=2&sa=false
Header:  Authorization: Bearer <access_token>
         Origin: https://maps.garmin.com
         Referer: https://maps.garmin.com/
```

Significato dei parametri (stessi nomi dello script SAS.Planet):

- `layer`: `0` = carta nautica (Seachart), `1` = SonarChart
- `transparent`: `false` = strato base opaco, `true` = overlay
- `ugc`: `true` = mostra i marker Active Captain (community Garmin), `false` = no
- `du`: unità profondità → `1` metri, `2` piedi, `3` braccia
- `sd`: safe depth (profondità di sicurezza)

### Gestione token nel proxy

- L'`access_token` è un JWT: il proxy ne legge il campo `exp` e **rinnova
  automaticamente** quando mancano < 120 secondi alla scadenza.
- Se un tassello risponde **401**, il proxy forza un refresh e riprova una volta.
- Su tassello mancante/errore restituisce un **PNG 1×1 trasparente**, così Cesium
  non riempie la console di errori.

---

## 4. Cosa NON funziona (vicoli ciechi già esplorati)

Per non far ripetere errori a chi riprende il progetto:

- ❌ `backend.navionics.io` → il dominio **non risolve più**.
- ❌ `backend.navionics.com/tile/get_key/...` → risponde **401** (vecchio schema
  navtoken, dismesso).
- ❌ `tile1.navionics.com/viewer/api/v1/tile/...` **senza** `Authorization: Bearer`
  → **401**. Il solo parametro `config` non basta.
- ❌ Schema "Bearer + parametro `config`" inventato a tavolino senza i token reali
  di Garmin → non autentica.
- ✅ Funziona **solo** la combinazione del Passo 1 + Passo 2 qui sopra.

---

## 5. File del progetto

| File | Ruolo |
|---|---|
| `proxy.py` | Proxy Flask con auto-refresh dei token Garmin/Navionics. |
| `index.html` | Visualizzatore CesiumJS. Carica solo lo strato Navionics dal proxy; globo nero sotto (nessuna mappa di base). |
| `requirements.txt` | Dipendenze Python: flask, flask-cors, requests. |
| `package.json` / `package-lock.json` | Dipendenza frontend: pacchetto `cesium`. |
| `.gitignore` | Esclude `node_modules/`, cache Python, file temporanei. |
| `README.md` | Guida d'uso. |
| `AGENTS.md` | Questo file: contesto e storia tecnica. |

`node_modules/` (CesiumJS) **non è versionato**: si ricrea con `npm install`.

---

## 6. Setup su un nuovo computer

```bash
git clone https://github.com/mrhz1973/Planet-Clone.git
cd Planet-Clone

python -m pip install -r requirements.txt   # backend
npm install                                 # scarica CesiumJS in node_modules/

# Terminale 1
python proxy.py
# Terminale 2
python -m http.server 8000
```

Apri **http://localhost:8000/index.html**.
Stato token/diagnostica: **http://localhost:5000/status**.

---

## 7. Fragilità nota / manutenzione

L'intera catena dipende dal **flusso web attuale di Garmin/Navionics**. Se loro
cambiano l'endpoint `getNavionicsTokens`, il formato dei token o l'URL dei
tasselli, occorrerà aggiornare `proxy.py`. Il riferimento incrociato più utile
per capire eventuali nuovi schemi è lo script `GetUrlScript.txt` dei pacchetti
mappe Navionics per **SAS.Planet** (community), che replica lo stesso flusso.

Uso personale/didattico: rispettare i Termini di Servizio di Navionics/Garmin.
