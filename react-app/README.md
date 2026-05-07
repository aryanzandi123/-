# ProPaths React islands

The backend is Flask + server-rendered HTML + D3 (~15K lines of vanilla
JS). A full React rewrite would be a multi-week undertaking and is not
necessary for current pain points.

Instead, this directory hosts **React islands** — isolated widgets
written in React + TypeScript, bundled by Vite, mounted into `<div>` nodes
the server template exposes. Each island is independent; adding one
doesn't force any changes to the rest of the frontend.

## First island: pipeline-events drawer

Shows every structured event emitted by `utils.observability.log_event`
for an in-flight job — router decisions, arrow/direction drift, chain
merge collisions, and everything else the content-aware validators log.

Consumes the existing `/api/stream/<protein>` SSE endpoint. No backend
change required: the events are already in the payload since the PR-4
SSE drawer work.

## Add a new island

1. Create `src/islands/<slug>/main.tsx` that mounts a component with
   `createRoot(...).render(...)`.
2. Add the entry to `vite.config.ts` under `rollupOptions.input`.
3. In the Flask template, include `<script type="module" src="/static/react/<slug>.js"></script>` after the island's mount-point `<div>`.

The Vite build writes to `../static/react/` so Flask serves the bundled
JS/CSS from `/static/react/*` without any dev-server dependency in
production.

## Commands

```bash
cd react-app
npm install            # or: npm ci  (once package-lock.json exists)
npm run dev            # Vite dev server with HMR at :5173
npm run build          # production build to ../static/react/
npm run typecheck      # tsc --noEmit
```

## Template wiring (copy-paste into a Jinja page)

```html
<!-- Mount point for the pipeline-events drawer. ``<protein>`` is the
     job's query protein; the main.tsx entry reads it from the id slug. -->
<div id="pipeline-events-{{ protein }}"></div>

<!-- Bundled React island. Built by ``npm run build`` into static/react/. -->
<script type="module" src="{{ url_for('static', filename='react/pipeline-events.js') }}"></script>
```

## Why islands, not full rewrite?

- **Risk**: the D3 visualization has ~9000 lines of fine-tuned interactions.
  Re-implementing those in React + a viz library (d3-react, visx) is a
  multi-week project where the payoff is purely architectural; the D3
  code works today.
- **Incremental**: new widgets (modals, drawers, forms) are written in
  React from day one. Over time the React surface grows naturally.
- **No dual maintenance**: the backend doesn't change. The SSE endpoint
  is the contract; both vanilla and React consumers read from it.
