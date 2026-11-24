"use client";

import * as Dialog from "@radix-ui/react-dialog";
import * as RTabs from "@radix-ui/react-tabs";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "@/lib/api";

// ---------------------------------------------------------------------------------------------
// Data loading
// ---------------------------------------------------------------------------------------------
export function useApi<T>(loader: () => Promise<T>, deps: unknown[], pollMs?: number | ((d: T | null) => number | null)) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const reload = useCallback(async () => {
    try {
      const d = await loaderRef.current();
      setData(d);
      setError(null);
      return d;
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, "ERROR", String(e)));
      return null;
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let cancelled = false;
    const tick = async () => {
      const d = await reload();
      const interval = typeof pollMs === "function" ? pollMs(d) : pollMs;
      if (!cancelled && interval) timer = setTimeout(tick, interval);
    };
    setLoading(true);
    void tick();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reload]);
  return { data, error, loading, reload, setData };
}

// ---------------------------------------------------------------------------------------------
// Layout primitives
// ---------------------------------------------------------------------------------------------
export function PageHeader({ title, subtitle, actions, crumbs }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; crumbs?: ReactNode }) {
  return (
    <div className="mb-6">
      {crumbs && <div className="mb-1 text-xs text-slate-500">{crumbs}</div>}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">{title}</h1>
          {subtitle && <div className="mt-1 text-sm text-slate-600">{subtitle}</div>}
        </div>
        {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
      </div>
    </div>
  );
}

export function Card({ title, actions, children, className = "", pad = true }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={`rounded-lg border border-slate-200 bg-white shadow-sm ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-2.5">
          <h2 className="text-sm font-semibold text-slate-800">{title}</h2>
          {actions && <div className="flex gap-2">{actions}</div>}
        </header>
      )}
      <div className={pad ? "p-4" : ""}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, tone = "slate", hint }: { label: string; value: ReactNode; tone?: "slate" | "red" | "amber" | "green" | "blue"; hint?: string }) {
  const tones = { slate: "text-slate-900", red: "text-red-700", amber: "text-amber-700", green: "text-green-700", blue: "text-blue-800" };
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-4 py-3 shadow-sm" title={hint}>
      <div className={`text-2xl font-semibold tabular-nums ${tones[tone]}`}>{value}</div>
      <div className="text-xs text-slate-500">{label}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Badges
// ---------------------------------------------------------------------------------------------
const BADGE: Record<string, string> = {
  PASS: "bg-green-100 text-green-800 ring-green-600/20", FAIL: "bg-red-100 text-red-800 ring-red-600/20",
  CHANGED: "bg-blue-100 text-blue-800 ring-blue-600/20", ERROR: "bg-fuchsia-100 text-fuchsia-800 ring-fuchsia-600/20",
  INCONCLUSIVE: "bg-amber-100 text-amber-800 ring-amber-600/20", SKIPPED: "bg-slate-100 text-slate-700 ring-slate-500/20",
  CRITICAL: "bg-red-600 text-white ring-red-700", HIGH: "bg-orange-100 text-orange-800 ring-orange-600/30",
  MEDIUM: "bg-amber-100 text-amber-800 ring-amber-600/20", LOW: "bg-slate-100 text-slate-700 ring-slate-500/20",
  APPROVED: "bg-green-100 text-green-800 ring-green-600/20", VERIFIED: "bg-green-100 text-green-800 ring-green-600/20",
  COMPLETED: "bg-slate-100 text-slate-800 ring-slate-500/20", RUNNING: "bg-blue-100 text-blue-800 ring-blue-600/20",
  QUEUED: "bg-slate-100 text-slate-700 ring-slate-500/20", FAILED: "bg-red-100 text-red-800 ring-red-600/20",
  CANCELLED: "bg-slate-100 text-slate-500 ring-slate-500/20", CANCELLING: "bg-slate-100 text-slate-500 ring-slate-500/20",
  DRAFT: "bg-slate-100 text-slate-700 ring-slate-500/20", PENDING_REVIEW: "bg-amber-100 text-amber-800 ring-amber-600/20",
  REJECTED: "bg-red-100 text-red-800 ring-red-600/20", UNVERIFIED: "bg-slate-100 text-slate-700 ring-slate-500/20",
  OPEN: "bg-red-50 text-red-800 ring-red-600/20", INVESTIGATING: "bg-amber-50 text-amber-800 ring-amber-600/20",
  FIX_PROPOSED: "bg-blue-50 text-blue-800 ring-blue-600/20", READY_FOR_RETEST: "bg-indigo-100 text-indigo-800 ring-indigo-600/20",
  RESOLVED: "bg-green-100 text-green-800 ring-green-600/20", ACCEPTED_RISK: "bg-purple-100 text-purple-800 ring-purple-600/20",
  CLOSED: "bg-slate-100 text-slate-600 ring-slate-500/20", EXPECTED: "bg-sky-100 text-sky-800 ring-sky-600/20",
  UNEXPECTED: "bg-amber-100 text-amber-900 ring-amber-600/30", PROHIBITED: "bg-red-100 text-red-800 ring-red-600/20",
  VIOLATION: "bg-red-100 text-red-800 ring-red-600/20",
};

export function Badge({ value, label }: { value: string | null | undefined; label?: string }) {
  if (!value) return <span className="text-slate-400">—</span>;
  return (
    <span className={`inline-flex items-center whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-semibold ring-1 ring-inset ${BADGE[value] ?? "bg-slate-100 text-slate-700 ring-slate-400/20"}`}>
      {label ?? value.replace(/_/g, " ")}
    </span>
  );
}

export function Decision({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-slate-400">—</span>;
  const c: Record<string, string> = { APPROVE: "text-green-700", DECLINE: "text-red-700", REVIEW: "text-amber-700", HOLD: "text-fuchsia-700", REFER: "text-blue-700" };
  return <span className={`font-mono text-xs font-semibold ${c[value] ?? ""}`}>{value}</span>;
}

export function OutcomeBanner({ outcome, text }: { outcome: string | null | undefined; text?: string }) {
  if (!outcome) return null;
  const style = outcome === "PASS" ? "border-green-300 bg-green-50 text-green-900" : outcome === "FAIL" ? "border-red-300 bg-red-50 text-red-900" : "border-amber-300 bg-amber-50 text-amber-900";
  return (
    <div className={`flex items-center gap-3 rounded-lg border px-4 py-3 ${style}`} role="status">
      <span className="text-lg font-bold">{outcome}</span>
      {text && <span className="text-sm">{text}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------------------------
export function Button({ children, variant = "secondary", size = "md", ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost"; size?: "sm" | "md" }) {
  const v = {
    primary: "bg-[#0b2545] text-white hover:bg-[#13315c] disabled:bg-slate-400",
    secondary: "bg-white text-slate-800 ring-1 ring-inset ring-slate-300 hover:bg-slate-50 disabled:text-slate-400",
    danger: "bg-red-700 text-white hover:bg-red-800 disabled:bg-slate-400",
    ghost: "text-slate-700 hover:bg-slate-100",
  }[variant];
  const s = size === "sm" ? "px-2 py-1 text-xs" : "px-3 py-1.5 text-sm";
  return <button type="button" {...props} className={`inline-flex items-center gap-1.5 rounded-md font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-600 disabled:cursor-not-allowed ${v} ${s} ${props.className ?? ""}`}>{children}</button>;
}

export function Field({ label, hint, children, error }: { label: string; hint?: string; children: ReactNode; error?: string }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block font-medium text-slate-700">{label}</span>
      {children}
      {hint && !error && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
      {error && <span className="mt-1 block text-xs text-red-700">{error}</span>}
    </label>
  );
}

export const inputCls = "block w-full rounded-md border-0 px-2.5 py-1.5 text-sm text-slate-900 ring-1 ring-inset ring-slate-300 placeholder:text-slate-400 focus:ring-2 focus:ring-inset focus:ring-blue-600";

export function Select({ value, onChange, options, placeholder, ...rest }: { value: string; onChange: (v: string) => void; options: { value: string; label: string }[]; placeholder?: string } & Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "onChange" | "value">) {
  return (
    <select {...rest} value={value} onChange={(e) => onChange(e.target.value)} className={inputCls}>
      {placeholder !== undefined && <option value="">{placeholder}</option>}
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

export function ErrorBox({ error }: { error: { message: string; details?: unknown } | null | undefined }) {
  if (!error) return null;
  const details: unknown = error.details;
  const lines: string[] = Array.isArray(details)
    ? details.map((d) => (typeof d === "string" ? d : (d as { message?: string })?.message ?? JSON.stringify(d)))
    : details && typeof details === "object"
      ? Object.values(details as Record<string, unknown>).flat().filter(Boolean).map((d) => String(d))
      : [];
  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-900" role="alert">
      <div className="font-medium">{error.message}</div>
      {lines.length > 0 && <ul className="mt-1 list-disc pl-5 text-xs">{lines.map((d, i) => <li key={i}>{d}</li>)}</ul>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return <div className="py-8 text-center text-sm text-slate-500" role="status">{label}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-md border border-dashed border-slate-300 px-4 py-6 text-center text-sm text-slate-500">{children}</div>;
}

export function Progress({ percent }: { percent: number }) {
  return (
    <div className="h-2 w-full overflow-hidden rounded bg-slate-200" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}>
      <div className="h-full bg-[#0b2545] transition-all" style={{ width: `${Math.min(100, percent)}%` }} />
    </div>
  );
}

export function Mono({ children }: { children: ReactNode }) {
  return <span className="whitespace-nowrap font-mono text-xs text-slate-700">{children}</span>;
}

export function Json({ value }: { value: unknown }) {
  return <pre className="max-h-80 overflow-auto rounded bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-100">{JSON.stringify(value, null, 2)}</pre>;
}

// ---------------------------------------------------------------------------------------------
// Table
// ---------------------------------------------------------------------------------------------
export function Table({ head, children, empty }: { head: ReactNode[]; children: ReactNode; empty?: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-sm">
        <thead className="bg-slate-50 text-left text-xs font-semibold uppercase tracking-wide text-slate-500">
          <tr>{head.map((h, i) => <th key={i} scope="col" className="whitespace-nowrap px-3 py-2">{h}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-slate-100">{children}</tbody>
      </table>
      {empty}
    </div>
  );
}

export function Td({ children, className = "" }: { children?: ReactNode; className?: string }) {
  return <td className={`px-3 py-2 align-top ${className}`}>{children}</td>;
}

// ---------------------------------------------------------------------------------------------
// Modal & tabs (Radix: focus trapping, Escape, aria attributes)
// ---------------------------------------------------------------------------------------------
export function Modal({ open, onOpenChange, title, description, children, wide }: { open: boolean; onOpenChange: (o: boolean) => void; title: string; description?: string; children: ReactNode; wide?: boolean }) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-slate-900/40" />
        <Dialog.Content className={`fixed left-1/2 top-[6vh] z-50 max-h-[88vh] w-[95vw] -translate-x-1/2 overflow-y-auto rounded-lg bg-white p-5 shadow-xl focus:outline-none ${wide ? "max-w-5xl" : "max-w-xl"}`}>
          <Dialog.Title className="text-base font-semibold text-slate-900">{title}</Dialog.Title>
          {description ? <Dialog.Description className="mt-1 text-sm text-slate-600">{description}</Dialog.Description> : <Dialog.Description className="sr-only">{title}</Dialog.Description>}
          <div className="mt-4">{children}</div>
          <Dialog.Close asChild>
            <button className="absolute right-3 top-3 rounded p-1 text-slate-500 hover:bg-slate-100" aria-label="Close">✕</button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function Tabs({ tabs, value, onChange }: { tabs: { value: string; label: ReactNode; content: ReactNode }[]; value?: string; onChange?: (v: string) => void }) {
  return (
    <RTabs.Root value={value} onValueChange={onChange} defaultValue={tabs[0]?.value}>
      <RTabs.List className="mb-4 flex gap-1 border-b border-slate-200" aria-label="Sections">
        {tabs.map((t) => (
          <RTabs.Trigger key={t.value} value={t.value} className="-mb-px border-b-2 border-transparent px-3 py-2 text-sm font-medium text-slate-600 hover:text-slate-900 data-[state=active]:border-[#0b2545] data-[state=active]:text-[#0b2545]">
            {t.label}
          </RTabs.Trigger>
        ))}
      </RTabs.List>
      {tabs.map((t) => <RTabs.Content key={t.value} value={t.value}>{t.content}</RTabs.Content>)}
    </RTabs.Root>
  );
}
