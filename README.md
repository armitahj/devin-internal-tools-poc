# devin-internal-tools-poc

Internal Tools Foundation prototype. See [specs/SPEC.md](specs/SPEC.md).

## Run

```bash
docker compose up        # Postgres 16 (host port 5433) + app at http://localhost:8000
curl localhost:8000/health
```

The app container runs `alembic upgrade head` before starting uvicorn.

## Test

Tests run against Postgres (not SQLite). With the compose `db` service running:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
docker compose up -d db
.venv/bin/pytest
```

The suite creates a `foundation_test` database if missing and migrates it to head.
Override the target with `TEST_DATABASE_URL`. CI (`.github/workflows/ci.yml`) runs
pytest on every pull request against a Postgres 16 service.

## Layout

- `foundation/` — shared package (currently only DB engine/session setup)
- `apps/` — FastAPI entrypoint (`apps/main.py`); per-app packages go here
- `alembic/` — migrations (`0001` is an empty initial revision)
- `tests/`
