/**
 * Barrel export for the typed API client — `import { ... } from "@/api"`.
 * Mirrors apps/mobile/src/api/index.ts's shape; only re-exports the
 * modules that exist in this app so far (campaign issue #234 — PR 1
 * shipped types/errors/client, PR 2 added queue/drafts/notifications, PR 3
 * added cases, PR 4 adds properties/tenants/trust). Later PRs (me) add
 * their own module and extend this list — never widen it ahead of a real
 * module existing.
 */
export * from "./types";
export * from "./errors";
export { apiRequest, apiRequestWithDate } from "./client";
export type { ApiResponseEnvelope } from "./client";
export * from "./queue";
export * from "./drafts";
export * from "./notifications";
export * from "./cases";
export * from "./properties";
export * from "./tenants";
export * from "./trust";
