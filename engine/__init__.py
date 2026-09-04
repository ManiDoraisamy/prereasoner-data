"""Prereasoner engine — the consolidated serving package.

One server (`python -m engine.server`) exposes three endpoints on one process:

  POST /api/reason    — composition reasoner (view-stacking) over the live world DB (Firebase auth + RTDB trace)
  POST /api/knowledge     — world path: unified-encoder world joins / hybrid semantic SQL (Firebase auth + RTDB trace)
  POST /api/dimension — authenticated stateless per-column/per-cell taxonomy readout (no Postgres)
  GET  /healthz       — liveness

Configuration is centralized in engine.config (env vars only; see docs/notes/engine.md).
"""
