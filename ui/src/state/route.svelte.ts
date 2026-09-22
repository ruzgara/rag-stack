// A two-route history router: "/" (a new chat in the current workspace) and
// "/c/<id>" (a conversation). Everything else in the UI is a popup over
// these, not a page. Runes only compile in *.svelte.ts files.

export interface Route {
  convId: string | null;
  notFound: boolean;
}

// Paths the SERVER owns; a click on a link to one must reach the browser.
const SERVER_OWNED = ["/admin", "/docs", "/redoc", "/openapi.json", "/api/", "/v1/"];

function match(path: string): Route {
  if (path === "/" || path === "") return { convId: null, notFound: false };
  const m = /^\/c\/([^/]+)\/?$/.exec(path);
  if (m) {
    try {
      return { convId: decodeURIComponent(m[1]), notFound: false };
    } catch {
      // A malformed escape (/c/%) is a bad link, not a crash.
    }
  }
  return { convId: null, notFound: true };
}

export const route: Route = $state(match(location.pathname));

export function navigate(url: string, opts: { replace?: boolean } = {}): void {
  if (url === location.pathname + location.search + location.hash) return;
  history[opts.replace ? "replaceState" : "pushState"]({}, "", url);
  Object.assign(route, match(location.pathname));
}

let initialized = false;

export function initRouter(): void {
  if (initialized) return;
  initialized = true;
  addEventListener("popstate", () => Object.assign(route, match(location.pathname)));
  // One delegated listener, so a plain <a href="/c/x"> works.
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
