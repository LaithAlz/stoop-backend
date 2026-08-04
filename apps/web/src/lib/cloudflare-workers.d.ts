// Ambient declaration for the Cloudflare Workers runtime's built-in
// `cloudflare:workers` module. We don't depend on
// `@cloudflare/workers-types` (the rest of the app deliberately keeps
// `env`/`ctx` typed as `unknown`, see src/server.ts). This is the minimal
// shape actually used, in src/routes/early-access.tsx's `getWaitlistDb`,
// which casts the import result itself before touching bindings.
//
// DELETE THIS FILE if `@cloudflare/workers-types` is ever added. Note that
// `skipLibCheck: true` in tsconfig.json means a competing `declare module
// "cloudflare:workers"` merges SILENTLY, with no diagnostic and no
// guarantee about which declaration wins (safety review, #267).
declare module "cloudflare:workers" {
  export const env: Record<string, unknown>;
}
