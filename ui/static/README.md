# ui/static

Files the UI serves at `/static/<name>` (only the names listed in `STATIC_FILES` in `ui/ui.py`). Vendored so the
dashboard works offline and never loads third-party code at run time.

| File | What | Source |
|---|---|---|
| `lightweight-charts.standalone.production.js` | TradingView Lightweight Charts v5.2.1 (Apache-2.0), the Trade page chart | npm `lightweight-charts@5.2.1`, `dist/`; tarball checked against the npm sha512 integrity |
| `lightweight-charts.LICENSE` | Its licence | same package |

sha256 of the .js file: `e21cc5caa0226ef30bd8549c50b9ef926615f2a4ee6b4e486353477a55f598cf` (tests/test_template.py checks it).
The chart shows the small TradingView attribution logo, as the licence's NOTICE asks.
To upgrade: take `dist/lightweight-charts.standalone.production.js` from the new npm version, verify the npm integrity hash,
replace the file, update the version and sha256 here and in the test.
