# constellation-web

The Constellation control-plane UI — React + Vite + TypeScript. Three pages, nothing else:

| Route         | Page     | What it does                                                     |
| ------------- | -------- | ---------------------------------------------------------------- |
| `/`           | Create   | one input ("create anything") + free / budget `$min–$max` toggle |
| `/run/:id`    | Pipeline | live SSE animation of the build pipeline, stage by stage         |
| `/result/:id` | Result   | deployed link · GitHub repo · golden-test result · credentials   |

Design: minimalist, **sharp squares** (0 radius everywhere), near-black + one electric-lime
accent, system fonts only (offline — no webfont fetch). HashRouter, so the gateway can serve
the build with no server-side deep-link fallback.

Not a uv workspace member (it's node, excluded in the root `pyproject.toml`).

## Dev

```bash
npm install
npm run dev        # http://localhost:5173 — proxies /api, /eval, /healthz to the gateway :8080
```

Start the gateway alongside it:

```bash
# repo root
uv run --group serve python -m constellation_gateway.main
```

## Build

```bash
npm run build      # tsc -b && vite build  ->  dist/
```

The gateway serves `dist/` at `/` when present (`create_app()` in `constellation_gateway.app`).
`docker build -f apps/gateway/Dockerfile .` builds this UI in a node stage and copies `dist`
into the gateway image, so the single image serves both the API and the UI.

## API it talks to

- `POST /api/create` `{prompt, budget:{mode,min,max}}` → `{run_id, slug}`
- `GET /api/stream/{run_id}` → SSE: `start` · `stage` · `log` · `done`
- `GET /api/result/{run_id}` → the final result object
