// A hand-rolled history-API router: three routes, no nested layouts, no
// dependency. See plan §4 for why (the Svelte 5 router ecosystem is still
// young; ~90 lines beats a compatibility gamble).
//
// Runes only compile in *.svelte.ts files -- this one is named accordingly.

type RouteName = "chat" | "settings" | "notfound";
export interface Match {
  name: RouteName;
  params: Record<string, string>;
}

const ROUTES: [RegExp, RouteName, string[]][] = [
  [/^\/$/, "chat", []],
  [/^\/c\/([^/]+)$/, "chat", ["id"]],
  [/^\/settings$/, "settings", []],
];

// Paths the SERVER owns. A history router that swallows these renders a
// blank route instead of letting the browser fetch the real page -- this is
// the clause that breaks the sidebar's /admin link if forgotten.
const SERVER_OWNED = ["/admin", "/docs", "/redoc", "/openapi.json", "/api/", "/v1/"];

function match(path: string): Match {
  for (const [re, name, keys] of ROUTES) {
    const m = re.exec(path);
    if (m) return { name, params: Object.fromEntries(keys.map((k, i) => [k, m[i + 1]])) };
  }
  return { name: "notfound", params: {} };
}

export const route: Match = $state(match(location.pathname));

/** `url` may include a search/hash; only the pathname drives route matching. */
export function navigate(url: string, opts: { replace?: boolean } = {}): void {
  if (url === location.pathname + location.search + location.hash) return;
  history[opts.replace ? "replaceState" : "pushState"]({}, "", url);
  Object.assign(route, match(location.pathname));
}

let initialized = false;

/** Called once from App.svelte. Kept out of module-eval time so tests that
 *  import this module (there aren't any today, but lib/ modules are held to
 *  that bar) don't register global listeners as a side effect of import. */
export function initRouter(): void {
  if (initialized) return;
  initialized = true;

  addEventListener("popstate", () => Object.assign(route, match(location.pathname)));

  // One delegated listener, so plain <a href="/c/x"> works with no <Link> component.
  addEventListener("click", (e: MouseEvent) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = (e.target as Element | null)?.closest("a");
    if (!a || !(a instanceof HTMLAnchorElement) || a.target || a.hasAttribute("download") || a.rel === "external") return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin) return;
    if (SERVER_OWNED.some((p) => url.pathname === p || url.pathname.startsWith(p))) return;
    e.preventDefault();
    navigate(url.pathname + url.search + url.hash);
  });
}
