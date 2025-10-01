# Web Scraper — v11
- Multi-site adapters (Shamy, EgyGamer, TheGameCaveEgypt, Compume, AHW, Compumarts, Games2Egypt, GamesWorldEgypt, GamersColony, EgyptLaptop, HardwareMarket, PCS-Souq, RabbitStore, TV-IT, EgyptGameStore).
- Category filters (PC accessories + controllers; excludes video games/gift cards/consoles).
- Name extraction: attributes + JSON-LD fallback.
- Seeds per domain + sitemap discovery + pagination.
- Default 500/site, CSV + JSONL outputs, commits to `data/` (PR fallback if branch protected).

## Sheets
```
=IMPORTDATA("https://raw.githubusercontent.com/<OWNER>/<REPO>/<BRANCH>/data/snapshot.csv")
```
