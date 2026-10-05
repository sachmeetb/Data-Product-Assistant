"""Data Workbench launcher CLI (`dwb`).

A single, purpose-built tool for launching the Workbench stack either directly
on the host (hot-reload dev) or via Docker Compose (reproducible), with opt-in
sample databases, one-click quick-connect prefill, graceful shutdown, and a
layered environment reset. Stdlib only — runnable with the system ``python3``
before any venv exists (so ``dwb doctor`` / ``dwb up --mode host`` can create the
venv themselves).

Entry point: ``python3 -m cli`` (the repo-root ``./dwb`` shim forwards to it).
"""
