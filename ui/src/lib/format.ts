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
