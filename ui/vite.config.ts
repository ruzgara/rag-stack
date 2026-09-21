import type { ProxyOptions } from "vite";
import { defineConfig } from "vitest/config"; // vitest's defineConfig merges in the `test` key
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Belt and braces for SSE. api.py already sets Cache-Control: no-cache and
// X-Accel-Buffering: no (api.py:848), and there is no GZipMiddleware in the
// app -- only SessionMiddleware -- so the usual buffering culprit is absent.
// What's left is the proxy's own Node HTTP client negotiating an encoding,
// so ask for identity explicitly. Typed loosely -- the proxy's real type
// comes from Node's http-proxy, which isn't worth a direct dependency here.
function configure(proxy: unknown): void {
  (proxy as { on: (event: string, cb: (req: { setHeader: (k: string, v: string) => void }) => void) => void }).on(
    "proxyReq",
    (req) => req.setHeader("Accept-Encoding", "identity"),
  );
}

const apiProxy: ProxyOptions = { target: "http://127.0.0.1:8088", changeOrigin: false, configure };

export default defineConfig({
  plugins: [svelte()],
  // Absolute asset URLs -- required, since the dev server's SPA fallback
  // (and the server's own catch-all in production) serves index.html at
  // arbitrary depths like /c/<id>.
  base: "/",
  build: {
    outDir: "dist",
    emptyOutDir: true, // stale hashed assets must not accumulate in a COMMITTED dir
    target: "es2022",
    sourcemap: false, // keeps the committed diff small
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": apiProxy,
      "/v1": apiProxy,
      "/admin": { target: "http://127.0.0.1:8088", changeOrigin: false },
    },
  },
  // Tests mount real components (tests/app.smoke.test.ts), which needs
  // Svelte's browser build rather than the server one Node would resolve.
  resolve: process.env.VITEST ? { conditions: ["browser"] } : undefined,
  test: {
    environment: "jsdom",
    include: ["src/tests/**/*.test.ts"],
  },
});
