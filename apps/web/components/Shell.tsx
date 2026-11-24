"use client";

import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { api } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import type { Project } from "@/lib/types";
import { Loading, useApi } from "./ui";

function NavLink({ href, children, exact }: { href: string; children: ReactNode; exact?: boolean }) {
  const path = usePathname();
  const active = exact ? path === href : path === href || path.startsWith(`${href}/`);
  return (
    <Link href={href} aria-current={active ? "page" : undefined}
          className={`block rounded-md px-3 py-1.5 text-sm ${active ? "bg-white/15 font-semibold text-white" : "text-slate-300 hover:bg-white/10 hover:text-white"}`}>
      {children}
    </Link>
  );
}

function ProjectNav({ id }: { id: string }) {
  const { data } = useApi(() => api.get<Project>(`/v1/projects/${id}`), [id]);
  const base = `/projects/${id}`;
  return (
    <div className="mt-6">
      <div className="px-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Project</div>
      <div className="truncate px-3 pb-2 text-sm text-white" title={data?.name}>{data?.name ?? "…"}</div>
      <nav className="space-y-0.5" aria-label="Project">
        <NavLink href={base} exact>Project setup</NavLink>
        <NavLink href={`${base}/connections`}>Connections</NavLink>
        <NavLink href={`${base}/builder`}>Policy &amp; test builder</NavLink>
        <NavLink href={`${base}/runs`}>Test execution</NavLink>
        <NavLink href={`${base}/findings`}>Findings</NavLink>
        <NavLink href={`${base}/release`}>Release review &amp; reports</NavLink>
      </nav>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const { me, loading, logout, has } = useAuth();
  const path = usePathname();
  const router = useRouter();
  const params = useParams<{ id?: string }>();
  const onLogin = path === "/login";

  useEffect(() => {
    if (!loading && !me && !onLogin) router.replace("/login");
  }, [loading, me, onLogin, router]);

  if (onLogin) return <>{children}</>;
  if (loading || !me) return <Loading label="Signing in…" />;

  return (
    <div className="flex min-h-screen">
      <aside className="flex w-60 shrink-0 flex-col bg-[#0b2545] px-3 py-4">
        <Link href="/" className="px-3 text-base font-bold tracking-tight text-white">ModelAssure</Link>
        <div className="px-3 text-[11px] text-slate-400">ReleaseGate · {me.organization.name}</div>
        <nav className="mt-6 space-y-0.5" aria-label="Main">
          <NavLink href="/" exact>Dashboard</NavLink>
          {has(...CAN.createProject) && <NavLink href="/projects/new">New project</NavLink>}
          {has(...CAN.audit) && <NavLink href="/audit">Audit trail</NavLink>}
        </nav>
        {params?.id && params.id !== "new" && <ProjectNav id={params.id} />}
        <div className="mt-auto border-t border-white/10 px-3 pt-3 text-xs text-slate-300">
          <div className="font-medium text-white">{me.name}</div>
          <div>{me.email}</div>
          <div className="mt-1 flex flex-wrap gap-1">{me.roles.map((r) => <span key={r} className="rounded bg-white/10 px-1.5 py-0.5 text-[10px]">{r.replace("_", " ")}</span>)}</div>
          <button onClick={logout} className="mt-2 text-slate-300 underline hover:text-white">Sign out</button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 bg-slate-50 px-8 py-6">
        <div className="mx-auto max-w-7xl">{children}</div>
        <footer className="mx-auto mt-10 max-w-7xl border-t border-slate-200 pt-3 text-xs text-slate-500">
          ModelAssure supports human release decisions. It is not a regulator and never authorises production deployment.
        </footer>
      </main>
    </div>
  );
}
