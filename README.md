# IT-Stellen

Persönliche Übersicht offener IT-Stellen im Raum Baden / Zürich / Brugg, mit Schwerpunkt Client- und Endpoint-Management (Jamf, Intune), Microsoft 365, Support, Systemtechnik und Automatisierung. Jede Stelle bekommt einen Match-Score und lässt sich nach Thema, Pensum, Umkreis und Rolle filtern.

- **Webseite:** `docs/index.html` (wird über GitHub Pages veröffentlicht)
- **Daten:** `docs/jobs.json`, automatisch erzeugt von `scraper/fetch_jobs.py`
- **Automatik:** `.github/workflows/update.yml` läuft zweimal täglich auf GitHub

## Quellen

| Quelle | Inhalt |
|---|---|
| jobs.ch | Öffentliche Such-API, Kantone Zürich und Aargau |
| job-room.ch | Stellenbörse des Bundes (AG, ZH) |
| ag.ch/stellen | Kanton Aargau, Rubrik Informatik |
| jobs.ethz.ch | ETH Zürich, IT-Stellen |

Suchbegriffe, Themen und die Gewichtung für den Match-Score stehen oben in `scraper/fetch_jobs.py` (`KEYWORDS`, `TOPICS`, `SKILLS`).

## Lokal testen

```bash
python3 scraper/fetch_jobs.py          # alle Quellen
python3 scraper/fetch_jobs.py jc       # nur jobs.ch (agk, eth, jc, jr)
cd docs && python3 -m http.server 8000 # dann http://localhost:8000
```

Nur Python-Standardbibliothek, keine Installation nötig.
