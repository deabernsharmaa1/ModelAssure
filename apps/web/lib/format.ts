export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-MY", { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function pct(x: number | null | undefined, digits = 1): string {
  return x === null || x === undefined ? "—" : `${(x * 100).toFixed(digits)}%`;
}

export function myr(x: number | null | undefined): string {
  return x === null || x === undefined ? "—" : `MYR ${Math.round(x).toLocaleString("en-MY")}`;
}

export function counts(d: Record<string, number> | null | undefined): string {
  if (!d || !Object.keys(d).length) return "—";
  return Object.entries(d).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ×${v}`).join(" · ");
}

export function short(hash: string | null | undefined, n = 10): string {
  return hash ? `${hash.slice(0, n)}…` : "—";
}

export function humanize(s: string): string {
  return s.toLowerCase().replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
