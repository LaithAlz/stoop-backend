// Ambient declaration for the Cloudflare Workers runtime's built-in
// `cloudflare:workers` module. We don't depend on `@cloudflare/workers-types`
// (the rest of the app deliberately keeps `env`/`ctx` typed as `unknown`,
// see src/server.ts) — this is the minimal shape actually used, in
// src/routes/early-access.tsx's `getWaitlistDb`, which casts the import
// result itself before touching bindings.
declare module "cloudflare:workers" {
  export const env: Record<string, unknown>;
}
