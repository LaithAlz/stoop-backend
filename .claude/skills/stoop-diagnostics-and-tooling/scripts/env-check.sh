#!/usr/bin/env bash
# env-check.sh -- Stoop toolchain + environment sanity check.
#
# SAFE BY CONSTRUCTION: never prints a secret VALUE. .env is inspected only
# with grep -c against variable NAMES; the file's contents never reach stdout.
# (Never `source .env` and never `cat .env` -- a sourced .env once echoed a
# live Twilio token into a terminal log and forced a credential rotation.)
#
# Usage (from repo root):
#   .claude/skills/stoop-diagnostics-and-tooling/scripts/env-check.sh
#
# Exit code = number of failed checks (0 = all good).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
ENV_FILE="$REPO_ROOT/apps/api/.env"
COMPOSE_FILE="$REPO_ROOT/docker-compose.yml"

FAILURES=0
pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }
info() { printf 'INFO  %s\n' "$1"; }

# macOS has no coreutils `timeout`; docker CLI calls can hang for minutes
# when the Docker Desktop daemon is wedged (a known local failure mode --
# recovery: pkill -f Docker; open -a Docker; wait; docker compose up -d).
# Bound every docker call so this script never hangs.
with_timeout() { # with_timeout <seconds> <cmd...>
  perl -e 'alarm shift; exec @ARGV' "$@" 2>/dev/null
}

# --- 1. uv ------------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  pass "uv present ($(uv --version 2>/dev/null | head -1))"
else
  fail "uv not found on PATH (needed for every apps/api command)"
fi

# --- 2. docker daemon -------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  if with_timeout 15 docker info >/dev/null; then
    pass "docker daemon reachable"

    # --- 3. compose postgres container health -------------------------------
    CID="$(with_timeout 15 docker compose -f "$COMPOSE_FILE" ps -q postgres || true)"
    if [ -n "$CID" ]; then
      HEALTH="$(with_timeout 15 docker inspect -f '{{.State.Health.Status}}' "$CID" || echo unknown)"
      if [ "$HEALTH" = "healthy" ]; then
        pass "compose postgres container healthy"
      else
        fail "compose postgres container state: ${HEALTH:-unknown} (from repo root: docker compose up -d)"
      fi
    else
      fail "compose postgres container not running (from repo root: docker compose up -d)"
    fi
  else
    fail "docker daemon not reachable within 15s (wedged? try: pkill -f Docker; open -a Docker; wait; docker compose up -d)"
    # Fallback signal: is anything at least listening on 5432?
    if nc -z -w 2 localhost 5432 >/dev/null 2>&1; then
      info "something IS listening on localhost:5432 (docker CLI may be wedged while the container still runs)"
    else
      info "nothing is listening on localhost:5432"
    fi
  fi
else
  fail "docker CLI not found on PATH"
fi

# --- 4. .env: required var NAMES only (values never printed) -----------------
# Required = fields declared with no default in apps/api/app/config.py.
REQUIRED_VARS="DATABASE_URL SUPABASE_URL SUPABASE_JWKS_URL SUPABASE_JWT_ISSUER SUPABASE_SERVICE_ROLE_KEY TWILIO_AUTH_TOKEN ANTHROPIC_API_KEY"
# Optional = defaulted/nullable fields (APP_DATABASE_URL and PUBLIC_BASE_URL
# become required when ENVIRONMENT=production -- config.py boot gates).
OPTIONAL_VARS="ENVIRONMENT LOG_LEVEL APP_DATABASE_URL PUBLIC_BASE_URL LANGSMITH_API_KEY LANGSMITH_PROJECT SENTRY_DSN"

if [ -f "$ENV_FILE" ]; then
  pass "apps/api/.env exists"
  for var in $REQUIRED_VARS; do
    if [ "$(grep -c "^${var}=" "$ENV_FILE")" -ge 1 ]; then
      pass "required var present: $var"
    else
      fail "required var MISSING: $var (template: apps/api/.env.example)"
    fi
  done
  for var in $OPTIONAL_VARS; do
    if [ "$(grep -c "^${var}=" "$ENV_FILE")" -ge 1 ]; then
      info "optional var present: $var"
    else
      info "optional var unset:   $var"
    fi
  done
else
  fail "apps/api/.env missing (copy apps/api/.env.example and fill it in; never commit, source, or cat it)"
fi

echo
if [ "$FAILURES" -eq 0 ]; then
  echo "env-check: ALL CHECKS PASSED"
else
  echo "env-check: $FAILURES check(s) FAILED"
fi
exit "$FAILURES"
