# Factory Vite + React template

A minimal single-screen starter for Factory projects. React 19 + Vite +
TypeScript, plain CSS (no UI framework, zero extra runtime deps).

## Develop

```sh
bun install
bun run dev
```

Serves the app at http://localhost:5173 with hot reload
(`--host` exposes it on the LAN, so preview URLs reach it from a sandbox).

## Build

```sh
bun run build
```

Type-checks (`tsc -b`) and bundles to `dist/`. Deploy that folder anywhere
(Workers + Assets, Pages, a static host) — it is fully static.

## Layout

- `src/main.tsx` — entry point
- `src/App.tsx` — the single screen (dark `#0a0a0a` background, orange
  `#ea580c` accent)
- `src/index.css` — global styles
- `vite.config.ts` — Vite config (React plugin, port 5173)
