"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Field, Loading, Modal, PageHeader, Progress, Select, Table, Td, inputCls, useApi } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtDate } from "@/lib/format";
import type { PolicySet, Project, Run, SystemVersion, TestSuite } from "@/lib/types";

const CATEGORIES = ["NORMAL", "BOUNDARY", "MISSING_DATA", "DEPENDENCY_FAILURE", "CROSS_SYSTEM_CONFLICT", "VERSION_REGRESSION", "DUPLICATE", "INVALID_INPUT", "HISTORICAL_REPLAY"];

function QueueRun({ project, onQueued }: { project: Project; onQueued: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const suites = useApi(() => api.get<TestSuite[]>(`/v1/test-suites?project_id=${project.id}`), [project.id]);
  const policies = useApi(() => api.get<PolicySet[]>(`/v1/policy-sets?project_id=${project.id}`), [project.id]);
  const versions = useApi(() => api.get<SystemVersion[]>(`/v1/systems/${project.system_id}/versions`), [project.system_id]);
  const [f, setF] = useState({ suite: "", baseline: project.baseline_version_id, candidate: project.candidate_version_id, cps: "", bps: "", reason: "" });
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f) => (v: string) => setF((x) => ({ ...x, [k]: v }));
  useEffect(() => { if (!f.suite && suites.data?.length) set("suite")(suites.data[0].id); }, [suites.data, f.suite]);
  useEffect(() => {
    const approved = (policies.data ?? []).filter((p) => p.approval_status === "APPROVED").sort((a, b) => b.version - a.version);
    if (!f.cps && approved[0]) set("cps")(approved[0].id);
    if (!f.bps && approved[1]) set("bps")(approved[1].id);
  }, [policies.data, f.cps, f.bps]);

  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const r = await api.post<{ run_id: string }>("/v1/runs", {
        project_id: project.id, test_suite_id: f.suite, baseline_version_id: f.baseline, candidate_version_id: f.candidate,
        policy_set_id: f.cps, baseline_policy_set_id: f.bps || null, mode: "SANDBOX",
        excluded_categories: [...excluded], exclusion_reason: f.reason || null,
      }, { "Idempotency-Key": crypto.randomUUID() });
      setOpen(false);
      onQueued(r.run_id);
    } catch (e) { setError(e as ApiError); } finally { setBusy(false); }
  };
  const ps = (policies.data ?? []).map((p) => ({ value: p.id, label: `${p.name} v${p.version} — ${p.approval_status}` }));
  const vs = (versions.data ?? []).map((v) => ({ value: v.id, label: v.version }));
  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>Queue validation run</Button>
      <Modal open={open} onOpenChange={setOpen} wide title="Queue a validation run" description="The configuration is locked when the run is created. Runs execute only against verified sandbox connections.">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Test suite"><Select value={f.suite} onChange={set("suite")} options={(suites.data ?? []).map((s) => ({ value: s.id, label: `${s.name} v${s.version} (${s.case_count} cases)` }))} placeholder="Select…" /></Field>
          <div />
          <Field label="Baseline version"><Select value={f.baseline} onChange={set("baseline")} options={vs} /></Field>
          <Field label="Candidate version"><Select value={f.candidate} onChange={set("candidate")} options={vs} /></Field>
          <Field label="Candidate policy set" hint="Approved rules the candidate must satisfy"><Select value={f.cps} onChange={set("cps")} options={ps} placeholder="Select…" /></Field>
          <Field label="Baseline policy set" hint="Rules in force for the baseline; used to recognise approved policy changes"><Select value={f.bps} onChange={set("bps")} options={ps} placeholder="Same as candidate" /></Field>
        </div>
        <fieldset className="mt-4"><legend className="mb-1 text-sm font-medium text-slate-700">Exclude categories (results become SKIPPED)</legend>
          <div className="grid grid-cols-2 gap-1 md:grid-cols-3">
            {CATEGORIES.map((c) => <label key={c} className="flex items-center gap-2 text-xs"><input type="checkbox" checked={excluded.has(c)} onChange={() => { const n = new Set(excluded); if (n.has(c)) n.delete(c); else n.add(c); setExcluded(n); }} />{c.replace(/_/g, " ").toLowerCase()}</label>)}
          </div>
          {excluded.size > 0 && <div className="mt-2"><Field label="Documented reason for exclusion"><input className={inputCls} value={f.reason} onChange={(e) => set("reason")(e.target.value)} /></Field></div>}
        </fieldset>
        <div className="mt-4 space-y-3"><ErrorBox error={error} />
          <Button variant="primary" onClick={submit} disabled={busy || !f.suite || !f.cps}>{busy ? "Queuing…" : "Queue run"}</Button></div>
      </Modal>
    </>
  );
}

export default function RunsPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { has } = useAuth();
  const project = useApi(() => api.get<Project>(`/v1/projects/${id}`), [id]);
  const runs = useApi(() => api.get<Run[]>(`/v1/runs?project_id=${id}`), [id],
                      (d) => (d?.some((r) => ["QUEUED", "RUNNING", "CANCELLING"].includes(r.status)) ? 2000 : null));
  if (project.loading) return <Loading />;
  return (
    <>
      <PageHeader title="Test execution" subtitle="Step 5: run the suite against baseline and candidate in the authorised sandbox."
                  actions={project.data && has(...CAN.createRun) ? <QueueRun project={project.data} onQueued={(rid) => router.push(`/projects/${id}/runs/${rid}`)} /> : null} />
      <ErrorBox error={runs.error} />
      <Card pad={false}>
        <Table head={["Run", "Versions", "Scope", "Status", "Progress / results", "Assessment", "Created"]}
               empty={!runs.data?.length && <div className="p-4"><Empty>No runs yet. Queue the first validation run.</Empty></div>}>
          {runs.data?.map((r) => (
            <tr key={r.id} className="hover:bg-slate-50">
              <Td><Link href={`/projects/${id}/runs/${r.id}`} className="font-medium text-blue-800 hover:underline">#{r.run_number}</Link></Td>
              <Td className="text-xs">{r.config.versions.baseline.version} → <b>{r.config.versions.candidate.version}</b></Td>
              <Td className="text-xs">{r.scope}{r.parent_run_id ? " (retest)" : ""}</Td>
              <Td><Badge value={r.status} /></Td>
              <Td className="w-64">{r.summary ? (
                <span className="text-xs tabular-nums">{r.summary.total} cases · <span className="text-red-700">{r.summary.by_result.FAIL} fail</span> · <span className="text-blue-700">{r.summary.by_result.CHANGED} changed</span> · {r.summary.by_result.ERROR} error · {r.summary.by_result.INCONCLUSIVE} inconcl.</span>
              ) : <div className="space-y-1"><Progress percent={r.progress.percent} /><span className="text-xs text-slate-500">{r.progress.completed}/{r.progress.total}</span></div>}</Td>
              <Td><Badge value={r.assessment?.outcome} /></Td>
              <Td className="text-xs text-slate-500">{fmtDate(r.created_at)}</Td>
            </tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
