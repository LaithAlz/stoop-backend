# stoop-api

FastAPI backend for Stoop — sorts tenant texts, drafts replies in the
landlord's voice, and rings the landlord only for true emergencies.

```bash
uv sync                                # install deps
uv run uvicorn app.main:app --reload   # run dev server (app skeleton lands in #5)
uv run pytest                          # tests
```
