/**
 * Builds one DOM-based harness entry (see entries/*.tsx and this
 * directory's README.md) into a browser IIFE bundle under dist/, using
 * the real app source (src/features/queue, src/routes/app.index.tsx,
 * src/routes/app.conversations.$id.tsx, ...) with only five things
 * shimmed out (see shims/), auth, Supabase, env, the toast library, and
 * TanStack Router's own route helpers. Everything else, including
 * `useDraftActions`, `queueEntriesReducer`, `buildQueueView`,
 * `unverifiedSendStore`, and the actual route components, is the real,
 * unmodified app code.
 *
 * Usage:
 *   bun run build.ts <entry-name-without-extension>
 *   # e.g.
 *   bun run build.ts entry_r4a
 *
 * Then serve dist/ and open it in headless Chrome (see README.md's
 * "Running an entry" section and run_entry_once.py, which does both in
 * one step via Playwright).
 */
import { type BunPlugin } from "bun";

const HERE = import.meta.dir;
const WEB = HERE + "/..";
const SRC = WEB + "/src";

const entry = process.argv[2];
if (!entry) {
  console.log("usage: bun run build.ts <entry-name-without-extension>");
  console.log("  entries live in ./entries, see README.md for what each one reproduces");
  process.exit(1);
}

const shims: Record<string, string> = {
  "@/lib/env": HERE + "/shims/env.ts",
  "@/lib/supabase": HERE + "/shims/supabase.ts",
  "@/auth/AuthProvider": HERE + "/shims/AuthProvider.tsx",
  sonner: HERE + "/shims/sonner.tsx",
  "@tanstack/react-router": HERE + "/shims/router.tsx",
};

const resolver: BunPlugin = {
  name: "stoop-harness-resolver",
  setup(build) {
    build.onResolve({ filter: /.*/ }, (args) => {
      const p = args.path;
      if (shims[p]) return { path: shims[p] };
      if (p.startsWith("@/")) return { path: Bun.resolveSync("./" + p.slice(2), SRC) };
      if (p.startsWith(".") || p.startsWith("/")) return undefined;
      return { path: Bun.resolveSync(p, WEB) };
    });
  },
};

const res = await Bun.build({
  entrypoints: [HERE + "/entries/" + entry + ".tsx"],
  outdir: HERE + "/dist",
  target: "browser",
  format: "iife",
  plugins: [resolver],
  define: { "process.env.NODE_ENV": '"development"' },
  sourcemap: "none",
});
if (!res.success) {
  for (const l of res.logs) console.log(String(l));
  process.exit(1);
}

// A minimal host page next to the bundle, same shape run_entry_once.py
// expects (`#app` to mount into, `#out` for the entry's own text log).
await Bun.write(
  HERE + `/dist/index_${entry}.html`,
  `<!doctype html><html><body><div id="app"></div><pre id="out">PENDING</pre><script src="${entry}.js"></script></body></html>`,
);

console.log(
  "built:",
  res.outputs.map((o) => o.path),
  "-> open dist/index_" + entry + ".html",
);
