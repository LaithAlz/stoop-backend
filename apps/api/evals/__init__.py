"""Eval harness (#35/#36) — scenario loader + runner for the 10 approved
scenarios in ``docs/02-product/eval-scenarios-v1.md`` plus the negative
Tier-0 prefilter suite required by ``docs/02-product/emergency-prefilter.md``.

Layout
------
``evals/scenario.py``   -- Pydantic scenario model + YAML loader (drift
                           protection in both directions: unknown fields
                           reject loudly).
``evals/scenarios/``     -- one YAML file per scenario, lifted verbatim from
                           the doc (``evals/scenarios/negative_prefilter/``
                           holds the R-class "detector chirping" suite).
``evals/context.py``     -- bridges the doc's simplified YAML context block
                           to the real node's context types
                           (``WeatherSnapshot``, ``VulnerableOccupant``, the
                           ``heating_season`` dict shape, etc).
``evals/fixtures.py``    -- the single voice-profile fixture used for every
                           scenario's draft call.
``evals/judge.py``       -- the LLM-as-judge grader. Its prompt is EVAL
                           INFRASTRUCTURE, not a product prompt -- see that
                           module's docstring for the governance distinction
                           from ``app/agent/prompts/v1.py``.
``evals/scoring.py``     -- per-scenario assertion logic + the release
                           -blocker gate (E-class/F-class hard-fail;
                           U-class/R-class soft-fail) per eval-scenarios
                           -v1.md's "Scoring & process" section.
``evals/runner.py``      -- orchestrates real (or, with ``EVAL_DRY_RUN=1``,
                           stubbed) Anthropic calls per scenario and exposes
                           a ``main()`` CLI entry point. **Read that
                           module's docstring before running anything** --
                           it documents exactly which invocation costs
                           money.

Never run for real from an automated agent without explicit human/
orchestrator sign-off -- see ``evals/runner.py``'s module docstring.
"""

from __future__ import annotations
