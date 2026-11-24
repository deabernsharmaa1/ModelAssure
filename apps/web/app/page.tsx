"use client";

import Link from "next/link";
import { Badge, Card, Empty, ErrorBox, Loading, PageHeader, Stat, Table, Td, useApi } from "@/components/ui";
import { api } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtDate } from "@/lib/format";
import type { Finding, Project, Run } from "@/lib/types";

export default function Dashboard() {
  const { has, me } = useAuth();
  const projects = useApi(() => api.get<Project[]>("/v1/projects"), []);
  const runs = useApi(() => api.get<Run[]>("/v1/runs"), [], 10000);
  const open = useApi(() => api.get<{ total: number; items: Finding[] }>("/v1/findings?status=OPEN&limit=50"), []);

  const pendingReviews = (runs.data ?? []).filter((r) => r.status === "COMPLETED" && r.scope === "FULL" && r.review_count === 0);
  const critical = (open.data?.items ?? []).filter((f) => f.severity === "CRITICAL");
  const nameOf = (pid: string) => projects.data?.find((p) => p.id === pid)?.name ?? "—";

  return (
    <>
      <PageHeader title="Dashboard" subtitle={`Release assurance overview for ${me?.organization.name}`}
                  actions={has(...CAN.createProject) ? <Link href="/projects/new" className="rounded-md bg-[#0b2545] px-3 py-1.5 text-sm font-medium text-white hover:bg-[#13315c]">New validation project</Link> : null} />
      <ErrorBox error={projects.error ?? runs.error ?? open.error} />
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Active projects" value={projects.data?.filter((p) => p.status === "ACTIVE").length ?? "…"} />
        <Stat label="Open findings" value={open.data?.total ?? "…"} tone={open.data?.total ? "red" : "slate"} />
        <Stat label="Open critical findings" value={critical.length} tone={critical.length ? "red" : "green"} />
        <Stat label="Runs awaiting review" value={pendingReviews.length} tone={pendingReviews.length ? "amber" : "slate"} />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Projects" pad={false}>
          {projects.loading ? <Loading /> : (
            <Table head={["Project", "Risk", "Open findings", "Latest run"]}
                   empty={!projects.data?.length && <div className="p-4"><Empty>No projects yet.</Empty></div>}>
              {projects.data?.map((p) => (
                <tr key={p.id} className="hover:bg-slate-50">
                  <Td><Link href={`/projects/${p.id}`} className="font-medium text-blue-800 hover:underline">{p.name}</Link>
                    <div className="text-xs text-slate-500">{p.test_environment}</div></Td>
                  <Td><Badge value={p.risk_classification === "HIGH" ? "HIGH" : p.risk_classification === "MEDIUM" ? "MEDIUM" : "LOW"} /></Td>
                  <Td className="tabular-nums">{p.open_findings ?? 0}</Td>
                  <Td>{p.latest_run ? (
                    <Link href={`/projects/${p.id}/runs/${p.latest_run.id}`} className="flex items-center gap-2 hover:underline">
                      #{p.latest_run.run_number} <Badge value={p.latest_run.assessment ?? p.latest_run.status} />
                    </Link>) : <span className="text-slate-400">—</span>}</Td>
                </tr>
              ))}
            </Table>
          )}
        </Card>

        <Card title="Pending release reviews" pad={false}>
          <Table head={["Run", "Project", "Assessment", "Completed"]}
                 empty={!pendingReviews.length && <div className="p-4"><Empty>No completed full runs are waiting for a reviewer decision.</Empty></div>}>
            {pendingReviews.slice(0, 8).map((r) => (
              <tr key={r.id} className="hover:bg-slate-50">
                <Td><Link href={`/projects/${r.project_id}/release?run=${r.id}`} className="font-medium text-blue-800 hover:underline">#{r.run_number}</Link>
                  <div className="text-xs text-slate-500">{r.config.versions.baseline.version} → {r.config.versions.candidate.version}</div></Td>
                <Td className="max-w-[14rem] truncate">{nameOf(r.project_id)}</Td>
                <Td><Badge value={r.assessment?.outcome} /></Td>
                <Td className="text-xs text-slate-500">{fmtDate(r.completed_at)}</Td>
              </tr>
            ))}
          </Table>
        </Card>

        <Card title="Open findings" pad={false}>
          <Table head={["Finding", "Severity", "Cases", "Project"]}
                 empty={!open.data?.items.length && <div className="p-4"><Empty>No open findings.</Empty></div>}>
            {open.data?.items.slice(0, 10).map((f) => (
              <tr key={f.id} className="hover:bg-slate-50">
                <Td><Link href={`/projects/${f.project_id}/findings?f=${f.id}`} className="text-blue-800 hover:underline">{f.key}</Link>
                  <div className="max-w-md truncate text-xs text-slate-600">{f.title}</div></Td>
                <Td><Badge value={f.severity} /></Td>
                <Td className="tabular-nums">{f.affected_count}</Td>
                <Td className="max-w-[12rem] truncate text-xs">{nameOf(f.project_id)}</Td>
              </tr>
            ))}
          </Table>
        </Card>

        <Card title="Latest runs" pad={false}>
          <Table head={["Run", "Status", "Results", "Created"]}
                 empty={!runs.data?.length && <div className="p-4"><Empty>No runs yet.</Empty></div>}>
            {runs.data?.slice(0, 8).map((r) => (
              <tr key={r.id} className="hover:bg-slate-50">
                <Td><Link href={`/projects/${r.project_id}/runs/${r.id}`} className="font-medium text-blue-800 hover:underline">#{r.run_number}</Link>
                  <span className="ml-1 text-xs text-slate-500">{r.scope === "RETEST" ? "retest" : ""}</span></Td>
                <Td><Badge value={r.status} /></Td>
                <Td className="text-xs tabular-nums">{r.summary ? `${r.summary.by_result.FAIL} fail · ${r.summary.by_result.CHANGED} changed · ${r.summary.by_result.ERROR} error` : `${r.progress.percent}%`}</Td>
                <Td className="text-xs text-slate-500">{fmtDate(r.created_at)}</Td>
              </tr>
            ))}
          </Table>
        </Card>
      </div>
    </>
  );
}
