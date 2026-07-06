-- db-probes.sql -- READ-ONLY diagnostic probe pack for the Stoop schema.
-- ============================================================================
-- FOR THE LOCAL DOCKER POSTGRES ONLY, or an EXPLICITLY-AUTHORIZED live read.
-- Never point this at the live Supabase pooler (*.pooler.supabase.com)
-- without the founder's go-ahead -- live-DB discipline is owned by the
-- stoop-run-and-operate skill. Every statement below is a SELECT (or \echo);
-- no writes, no DDL, no locks beyond ordinary reads.
--
-- Run against local docker (from repo root):
--   docker compose exec -T postgres psql -U stoop -d stoop \
--     < .claude/skills/stoop-diagnostics-and-tooling/scripts/db-probes.sql
-- or with a local psql client:
--   PGPASSWORD=stoop psql -h localhost -p 5432 -U stoop -d stoop \
--     -f .claude/skills/stoop-diagnostics-and-tooling/scripts/db-probes.sql
--
-- A probe erroring with "relation ... does not exist" means migrations are
-- not at head on this database: cd apps/api && uv run alembic upgrade head
-- ============================================================================

\echo '=== 0. migration head (compare with ls apps/api/migrations/versions/) ==='
SELECT version_num AS alembic_head FROM alembic_version;

\echo ''
\echo '=== 1. RLS status per table ==='
-- EXPECT (migrations at head, 0005 applied): every schema-v1 table in
-- public (13 tables; alembic_version is the one exception at f/f; the
-- LangGraph checkpointer tables live in the dedicated non-public
-- 'langgraph' schema per migration 0007, so they never appear here) has
-- rls_enabled = t and rls_forced = f.
-- ENABLE-not-FORCE is deliberate: FORCE broke /v1/me first-login
-- provisioning during the #22 design phase (the INSERT precedes any
-- landlord identity existing to scope by) -- do not "upgrade" to FORCE.
SELECT c.relname AS table_name,
       c.relrowsecurity   AS rls_enabled,
       c.relforcerowsecurity AS rls_forced
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relname;

\echo ''
\echo '=== 2. RLS policies ==='
-- EXPECT: one FOR ALL TO app_role policy per RLS table, USING + WITH CHECK
-- keyed on current_setting('app.current_landlord_id', true)::uuid
-- (landlords keys on id; message_cases / message_status_events join
-- through cases / messages -- migration 0005 step 3).
SELECT tablename, policyname, roles, cmd,
       (qual IS NOT NULL)       AS has_using,
       (with_check IS NOT NULL) AS has_with_check
FROM pg_policies
WHERE schemaname = 'public'
ORDER BY tablename, policyname;

\echo ''
\echo '=== 3. roles: superuser / bypassrls / canlogin ==='
-- EXPECT locally: role "stoop" is the bootstrap superuser (docker runs as
-- superuser -- which is why local runs are BLIND to privilege bugs; role
-- migrations get a live Supabase dry-run before merge). app_role should be
-- rolcanlogin = f (NOLOGIN until the one-time operator step) and
-- rolbypassrls = f. On live Supabase, postgres and service_role are NOT
-- superusers but ARE rolbypassrls = t (migration 0005 "LIVE ROLE FACTS").
SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
FROM pg_roles
WHERE rolname NOT LIKE 'pg\_%'
ORDER BY rolname;

\echo ''
\echo '=== 4a. grants on the append-only tables (information_schema view) ==='
-- EXPECT: app_role has SELECT and INSERT only on messages / audit_log /
-- message_status_events -- never UPDATE or DELETE (never-break rule #2;
-- migration 0005 step 2 REVOKEs them explicitly).
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_schema = 'public'
  AND table_name IN ('messages', 'audit_log', 'message_status_events')
ORDER BY table_name, grantee, privilege_type;

\echo ''
\echo '=== 4b. append-only check, direct (has_table_privilege for app_role) ==='
-- EXPECT: granted = t for SELECT/INSERT rows, f for UPDATE/DELETE rows.
-- Returns zero rows (no error) if app_role does not exist yet.
SELECT t.tbl AS table_name, p.priv AS privilege,
       has_table_privilege(r.rolname, t.tbl, p.priv) AS granted
FROM pg_roles r,
     (VALUES ('messages'), ('audit_log'), ('message_status_events')) AS t(tbl),
     (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE')) AS p(priv)
WHERE r.rolname = 'app_role'
ORDER BY t.tbl, p.priv;

\echo ''
\echo '=== 5. row counts per schema-v1 table ==='
-- Exact counts; fine at local-dev data volumes. The 13-table list is
-- canonical from docs/03-engineering/schema-v1.md / migration 0005.
SELECT 'landlords' AS table_name, count(*) AS rows FROM landlords
UNION ALL SELECT 'properties', count(*) FROM properties
UNION ALL SELECT 'vendors', count(*) FROM vendors
UNION ALL SELECT 'tenants', count(*) FROM tenants
UNION ALL SELECT 'cases', count(*) FROM cases
UNION ALL SELECT 'messages', count(*) FROM messages
UNION ALL SELECT 'message_cases', count(*) FROM message_cases
UNION ALL SELECT 'drafts', count(*) FROM drafts
UNION ALL SELECT 'trust_metrics', count(*) FROM trust_metrics
UNION ALL SELECT 'audit_log', count(*) FROM audit_log
UNION ALL SELECT 'notifications', count(*) FROM notifications
UNION ALL SELECT 'push_tokens', count(*) FROM push_tokens
UNION ALL SELECT 'message_status_events', count(*) FROM message_status_events
ORDER BY table_name;

\echo ''
\echo '=== 6a. latest audit_log actions (no payloads -- keep terminals PII-safe) ==='
-- actor is one of agent/landlord/system/prefilter; action vocabulary is the
-- CHECK constraint in migration 0002 (message_received, classified,
-- drafted, draft_stale, emergency_triggered, ...).
SELECT created_at, actor, action, case_id
FROM audit_log
ORDER BY created_at DESC, id DESC
LIMIT 20;

\echo ''
\echo '=== 6b. audit_log action histogram ==='
SELECT action, count(*) AS n
FROM audit_log
GROUP BY action
ORDER BY n DESC, action;

-- Deliberately NOT included by default: SELECT payload FROM audit_log.
-- Payloads are PII-free by design (severity, rules_fired, tokens, cost --
-- never message bodies or phone numbers), but dumping jsonb blobs to a
-- shared terminal invites pasting them into places they don't belong.
-- When you need one, target it:
--   SELECT payload FROM audit_log WHERE action = 'classified'
--   ORDER BY created_at DESC LIMIT 3;
