---
title: "chore(backend): configure ruff, mypy strict, pytest, and pre-commit"
labels: ["phase-1", "type-setup", "size-xs"]
milestone: "Phase 1: Backend Foundation"
---

## Goal

Lock in code quality tooling that runs locally on every commit and in CI on every PR.

## Why this matters

Strict tooling from day one prevents the slow drift toward "we'll clean it up later." Pre-commit hooks catch issues before CI.

## Acceptance criteria

- [ ] `[tool.ruff]` configured in `pyproject.toml` with line length 100 and a sensible lint rule set
- [ ] `[tool.mypy]` configured with strict mode and Python 3.12 target
- [ ] `[tool.pytest.ini_options]` configured with async mode and a custom marker for eval tests
- [ ] `.pre-commit-config.yaml` at repo root runs ruff, ruff-format, mypy, and a secret scanner
- [ ] `pre-commit install` is set up and hooks fire on commit
- [ ] `ruff check .` returns clean
- [ ] `mypy app` executes (will error because no `app/` code yet — that's fine, just verify it runs)
- [ ] `pytest` runs and reports "no tests collected" without error
- [ ] A test commit with a deliberate violation (e.g., unused import) is blocked by pre-commit

## Out of scope

- Don't set up GitHub Actions CI yet — issue #14
- Don't add commitlint or conventional-commit enforcement — low value at solo stage
- Don't add coverage gates — no code to cover yet

## Effort & dependencies

- **Effort:** XS (30-60 min)
- **Blocks:** All implementation issues from #5 onward
- **Blocked by:** #1

---

<details>
<summary><b>Hints</b></summary>

- The ruff rule sets you want: errors (`E`), Pyflakes (`F`), import sorting (`I`), naming (`N`), bugbear (`B`), pyupgrade (`UP`), async (`ASYNC`), security (`S`). Allow `assert` in tests via per-file-ignore for `S101`.
- mypy `strict = true` enables everything good. Add the Pydantic plugin (`plugins = ["pydantic.mypy"]`) and ignore-missing-imports for libs without type stubs (e.g. `twilio`).
- Pre-commit's mypy hook needs `pass_filenames: false` so it sees the whole app folder, not just changed files
- For secret scanning, gitleaks is faster and quieter than detect-secrets
- `asyncio_mode = "auto"` in pytest means you don't need `@pytest.mark.asyncio` on every test

</details>

<details>
<summary><b>Common gotchas</b></summary>

- mypy will complain about pydantic-settings field validation without the Pydantic plugin
- mypy doesn't love SQLAlchemy 2.0's declarative-style models by default — accept some `# type: ignore` in `db/models.py` later, or use the SQLAlchemy mypy plugin
- ruff and ruff-format do different things — both are needed in pre-commit
- Pre-commit hooks slow `git commit` by 5-15 seconds; don't bypass with `--no-verify` in normal flow

</details>

<details>
<summary><b>Review prompts for Claude Code</b></summary>

> Review my `pyproject.toml` ruff/mypy/pytest config and `.pre-commit-config.yaml`:
> 1. Are any rules I selected going to be noisy/unhelpful?
> 2. Is mypy strict mode going to fail on patterns I'll need (Pydantic, SQLAlchemy 2.0)?
> 3. Should I add other pre-commit hooks for a Python + Postgres project?

</details>
