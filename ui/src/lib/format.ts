// Small display-formatting helpers, kept pure so they need no test harness
// beyond plain assertions.

export function pageRange(pages: number[]): string {
  if (!pages.length) return "";
  const first = pages[0];
  const last = pages[pages.length - 1];
  return first === last ? `p. ${first}` : `p. ${first}–${last}`;
}

export function score(rerankScore: number | null): string {
  return rerankScore === null ? "" : `score ${rerankScore.toFixed(2)}`;
}

export function mb(n: number): string {
  return `${n.toFixed(1)} MB`;
}

export function byteSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const RTF = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

// Each boundary is "below this many seconds away, use this unit at this
// per-unit size" -- a standard graduated relative-time table.
const BOUNDARIES: [number, number, Intl.RelativeTimeFormatUnit][] = [
  [60, 1, "second"],
  [60 * 60, 60, "minute"],
  [60 * 60 * 24, 60 * 60, "hour"],
  [60 * 60 * 24 * 30, 60 * 60 * 24, "day"],
  [60 * 60 * 24 * 365, 60 * 60 * 24 * 30, "month"],
  [Infinity, 60 * 60 * 24 * 365, "year"],
];

/** epochSeconds is a Unix timestamp, as every *_at field in the API is. */
export function relTime(epochSeconds: number): string {
  const deltaSeconds = epochSeconds - Date.now() / 1000;
  const absSeconds = Math.abs(deltaSeconds);
  const [, unitSeconds, unit] = BOUNDARIES.find(([bound]) => absSeconds < bound) ?? BOUNDARIES[BOUNDARIES.length - 1];
  return RTF.format(Math.round(deltaSeconds / unitSeconds), unit);
}

/** "10:41" */
export function clock(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** The chat list's "when", as the design writes it: "2 minutes ago",
 *  "Today 09:12", "Yesterday", "Mon 16 Sep", "11 Sep", "3 Aug 2024". */
export function when(epochSeconds: number, now = Date.now()): string {
  const t = epochSeconds * 1000;
  const age = (now - t) / 1000;
  if (age < 60) return "just now";
  if (age < 3600) {
    const m = Math.floor(age / 60);
    return `${m} minute${m === 1 ? "" : "s"} ago`;
  }
  const d = new Date(t);
  const today = new Date(now);
  today.setHours(0, 0, 0, 0);
  const dayMs = 86_400_000;
  if (t >= today.getTime()) return `Today ${clock(epochSeconds)}`;
  if (t >= today.getTime() - dayMs) return "Yesterday";
  if (t >= today.getTime() - 6 * dayMs) {
    return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  }
  if (d.getFullYear() === today.getFullYear()) return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** "EM" for "Ellis Marchetti"; the email's first letters otherwise. */
export function initials(name: string | null, email: string): string {
  const words = (name || email.split("@")[0]).split(/[\s._-]+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[words.length - 1][0] : (words[0] ?? "?").slice(0, 2);
  return letters.toUpperCase();
}

/** "p. 24 · 11.3 Limitation of Liability" -- pages, then the innermost heading. */
export function locator(pages: number[], headings: string[]): string {
  const parts: string[] = [];
  const p = pageRange(pages);
  if (p) parts.push(p);
  if (headings.length) parts.push(headings[headings.length - 1]);
  return parts.join(" · ");
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}
