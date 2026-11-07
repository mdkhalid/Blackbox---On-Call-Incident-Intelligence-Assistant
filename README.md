# Blackbox — On-Call Incident Intelligence Assistant

Phase 0 (scaffolding) complete. See `docs/` for design and planning.

## Run locally
```bash
cp .env.example .env
pip install -e ".[dev]"
pytest -q
uvicorn blackbox.main:app --reload
```

## Run with Docker
```bash
cp .env.example .env
docker compose up --build
curl http://localhost:8000/health
```
