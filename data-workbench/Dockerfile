# Data Workbench backend (FastAPI + WebSocket + mounted MCP server at /mcp).
# The claude-agent-sdk wheel bundles the Claude Code binary, and the in-repo
# workbench-skills/ plugin bakes in here (loaded at runtime via --plugin-dir;
# skill scripts resolve via ${CLAUDE_SKILL_DIR}).
FROM python:3.12-slim

WORKDIR /app

# Dependencies first for layer caching. psycopg2-binary ships its own libpq and
# the claude-agent-sdk binary runs on glibc slim, so no apt build deps needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# dlt — in-backend execution of data-migration (dmig) packages. `Run Migration`
# runs the package with THIS interpreter, so dlt + its destination clients must
# be present. Installed as a separate step (not folded into the frozen lockfile)
# and CONSTRAINED by requirements.txt (`-c`) so it adds dlt + its own transitive
# deps WITHOUT disturbing any pinned version (notably databricks-sql-connector).
# mssql (pyodbc → needs a system ODBC driver) is intentionally omitted here —
# SQL Server migrations run via the downloadable package, not in-backend.
RUN pip install --no-cache-dir -c requirements.txt \
    "dlt[databricks,snowflake,postgres,sqlalchemy,parquet,s3]>=1.5,<2.0" "oracledb>=2.0"

# Bake the bge-small embedding model (ONNX, ~130 MB) into the image so the
# first Concept-Guided chat doesn't pay a HuggingFace download at runtime.
# Cached under FASTEMBED_CACHE_PATH; embeddings.py reads the same env var.
ENV FASTEMBED_CACHE_PATH=/app/.fastembed_cache
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='/app/.fastembed_cache')"

# App source (incl. the workbench-skills/ plugin). Runtime state (projects/, workbench.db)
# is excluded via .dockerignore and supplied by mounted volumes instead.
COPY . .

# Volume mount points: SQLite db (WB_DATABASE_URL points here) + project scratch.
RUN mkdir -p /app/data /app/projects

EXPOSE 8000
CMD ["uvicorn", "workbench.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
