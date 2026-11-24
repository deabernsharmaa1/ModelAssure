"use client";

import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { EvidenceModal } from "@/components/Evidence";
import { Badge, Button, Card, Decision, Empty, ErrorBox, Field, Loading, Modal, Mono, PageHeader, Select, Table, Td, inputCls, useApi } from "@/components/ui";
import { ApiError, api, qs } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { counts, fmtDate, humanize, myr } from "@/lib/format";
import type { Finding, Project, Run, SystemVersion, UserRef } from "@/lib/types";

const TRANSITIONS: Record<string, string[]> = {
  OPEN: ["INVESTIGATING", "FIX_PROPOSED", "ACCEPTED_RISK", "CLOSED"],
  INVESTIGATING: ["OPEN", "FIX_PROPOSED", "ACCEPTED_RISK", "CLOSED"],
  FIX_PROPOSED: ["INVESTIGATING", "READY_FOR_RETEST", "OPEN"],
  READY_FOR_RETEST: ["FIX_PROPOSED", "INVESTIGATING"],
  RESOLVED: ["CLOSED", "OPEN"], ACCEPTED_RISK: ["OPEN", "CLOSED"], CLOSED: ["OPEN"],
};

function Detail({ findingId, users, onChanged }: { findingId: string; users: UserRef[]; onChanged: () => void }) {
  const { has } = useAuth();
  const f = useApi(() => api.get<Finding>(`/v1/findings/${findingId}`), [findingId]);
  const [comment, setComment] = useState("");
  const [riskOpen, setRiskOpen] = useState(false);
  const [closeOpen, setCloseOpen] = useState(false);
  const [risk, setRisk] = useState({ owner_id: "", rationale: "", expires_on: "" });
  const [closure, setClosure] = useState({ reason: "VERIFIED_FIXED", rationale: "" });
  const [error, setError] = useState<ApiError | null>(null);
  const [evidence, setEvidence] = useState<string | null>(null);

  if (f.loading) return <Loading />;
  if (!f.data) return <ErrorBox error={f.error} />;
  const x = f.data;
  const patch = async (body: Record<string, unknown>) => {
    setError(null);
    try { await api.patch(`/v1/findings/${x.id}`, body); await f.reload(); onChanged(); setRiskOpen(false); setCloseOpen(false); setComment(""); }
    catch (e) { setError(e as ApiError); }
  };
  const next = (TRANSITIONS[x.status] ?? []).filter((s) => s !== "ACCEPTED_RISK" && s !== "CLOSED");
  const canEdit = has(...CAN.updateFinding);

  return (
    <div className="space-y-4">
      <Card title={<span className="flex flex-wrap items-center gap-2">{x.key} <Badge value={x.severity} /> <Badge value={x.status} /> <span className="text-xs font-normal text-slate-500">{humanize(x.type)} · run #{x.run_number}</span></span>}>
        <h3 className="mb-3 text-base font-semibold text-slate-900">{x.title}</h3>
        <dl className="grid gap-x-6 gap-y-3 text-sm md:grid-cols-2">
          <div><dt className="text-xs font-semibold uppercase text-slate-500">What failed</dt>
            <dd>{x.affected_count} case(s) · rules {x.rule_ids.map((r) => <Mono key={r}>{r} </Mono>)}<br />
              Expected: <b>{counts(x.summary.expected)}</b><br />Observed (candidate {x.summary.versions.candidate}): <b>{counts(x.summary.candidate_decisions)}</b><br />
              Baseline {x.summary.versions.baseline}: {counts(x.summary.baseline_decisions)}</dd></div>
          <div><dt className="text-xs font-semibold uppercase text-slate-500">Why</dt>
            <dd>{x.suspected_cause ?? <span className="text-slate-500">No cause is stated unless the evidence supports one.</span>}</dd></div>
          <div><dt className="text-xs font-semibold uppercase text-slate-500">How severe</dt>
            <dd><Badge value={x.severity} /> {x.suggested_severity !== x.severity && <span className="text-xs">(suggested {x.suggested_severity})</span>}
              <span className="ml-2 text-xs text-slate-600">Categories: {counts(x.summary.categories)} · requested amount on affected synthetic cases {myr(x.summary.exposure_requested_amount)}</span></dd></div>
          <div><dt className="text-xs font-semibold uppercase text-slate-500">Who owns it</dt>
            <dd>{canEdit ? (
              <Select aria-label="Owner" value={x.owner_id ?? ""} onChange={(v) => v && patch({ owner_id: v })} placeholder="Unassigned" options={users.map((u) => ({ value: u.id, label: `${u.name} (${u.roles.join(", ")})` }))} />
            ) : (x.owner ? `${x.owner.name} · ${x.owner.email}` : "Unassigned")}</dd></div>
          <div className="md:col-span-2"><dt className="text-xs font-semibold uppercase text-slate-500">Has it been corrected?</dt>
            <dd>{x.retest_history.length === 0 ? <span className="text-slate-600">Not yet retested.</span> : (
              <ul className="space-y-0.5">{x.retest_history.map((h, i) => (
                <li key={i}>Run #{h.run_number} on <b>{h.candidate_version}</b>: {h.passed ? <span className="font-semibold text-green-700">passed</span> : <span className="font-semibold text-red-700">still failing</span>} <span className="text-xs text-slate-500">({counts(h.outcomes)}) · {fmtDate(h.at)}</span></li>))}</ul>)}
              {x.risk_acceptance && x.status === "ACCEPTED_RISK" && <p className="mt-1 text-purple-800">Risk accepted by {x.risk_acceptance.authorised_by}; owner {x.risk_acceptance.owner_email}{x.risk_acceptance.expires_on ? `, expires ${x.risk_acceptance.expires_on}` : ""}: {x.risk_acceptance.rationale}</p>}
              {x.closure && x.status === "CLOSED" && <p className="mt-1 text-slate-700">Closed ({x.closure.reason}) by {x.closure.closed_by}: {x.closure.rationale}</p>}
            </dd></div>
        </dl>
        <ErrorBox error={error} />
        {canEdit && (
          <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-200 pt-4">
            {next.map((s) => <Button key={s} size="sm" variant={s === "READY_FOR_RETEST" ? "primary" : "secondary"} onClick={() => patch({ status: s })}>Move to {s.replace(/_/g, " ").toLowerCase()}</Button>)}
            {has(...CAN.acceptRisk) && TRANSITIONS[x.status]?.includes("ACCEPTED_RISK") && <Button size="sm" onClick={() => setRiskOpen(true)}>Accept risk…</Button>}
            {has(...CAN.closeFinding) && TRANSITIONS[x.status]?.includes("CLOSED") && <Button size="sm" onClick={() => setCloseOpen(true)}>Close…</Button>}
            <span className="text-xs text-slate-500">Resolved is set only by a passing retest.</span>
          </div>
        )}
        {canEdit && (
          <div className="mt-3 flex gap-2">
            <input aria-label="Comment" className={inputCls} placeholder="Add a comment for the investigation log" value={comment} onChange={(e) => setComment(e.target.value)} />
            <Button size="sm" disabled={!comment} onClick={() => patch({ comment })}>Comment</Button>
          </div>
        )}
      </Card>

      <Card title={`Affected cases (${x.affected_count})`} pad={false}>
        <Table head={["Case", "Category", "Baseline", "Candidate", "Expected", ""]}>
          {x.executions?.map((e) => (
            <tr key={e.id} className="hover:bg-slate-50">
              <Td><Mono>{e.case_key}</Mono></Td><Td className="text-xs">{e.category}</Td>
              <Td><Decision value={e.baseline_decision} /></Td><Td><Decision value={e.candidate_decision} /></Td>
              <Td className="text-xs">{e.violations.map((v) => String(v.expected)).join("; ") || e.error?.type || ""}</Td>
              <Td><Button size="sm" variant="ghost" onClick={() => setEvidence(e.id)}>Evidence</Button></Td>
            </tr>
          ))}
        </Table>
        {x.affected_count > (x.executions?.length ?? 0) && <p className="px-3 py-2 text-xs text-slate-500">Showing first {x.executions?.length}. All case keys are listed in the report.</p>}
      </Card>

      <Card title="Investigation history">
        <ol className="space-y-1 text-sm">
          {[...x.history].reverse().map((h, i) => (
            <li key={i}><span className="text-xs text-slate-500">{fmtDate(h.at)} · {h.by}</span> — {humanize(h.event)}
              {"status" in h && h.status ? <> → <Badge value={String(h.status)} /></> : null}{"text" in h ? `: ${String(h.text)}` : ""}{"owner" in h ? `: ${String(h.owner)}` : ""}</li>
          ))}
        </ol>
      </Card>

      <Modal open={riskOpen} onOpenChange={setRiskOpen} title="Document risk acceptance" description="The institution retains authority over business-risk acceptance. A named owner and rationale are required.">
        <div className="space-y-3">
          <Field label="Risk owner"><Select value={risk.owner_id} onChange={(v) => setRisk({ ...risk, owner_id: v })} placeholder="Select…" options={users.map((u) => ({ value: u.id, label: u.name }))} /></Field>
          <Field label="Rationale (min 20 characters)"><textarea className={inputCls} rows={3} value={risk.rationale} onChange={(e) => setRisk({ ...risk, rationale: e.target.value })} /></Field>
          <Field label="Expires on (optional)"><input type="date" className={inputCls} value={risk.expires_on} onChange={(e) => setRisk({ ...risk, expires_on: e.target.value })} /></Field>
          <ErrorBox error={error} />
          <Button variant="primary" onClick={() => patch({ status: "ACCEPTED_RISK", risk_acceptance: { owner_id: risk.owner_id, rationale: risk.rationale, expires_on: risk.expires_on || null } })}>Accept risk</Button>
        </div>
      </Modal>
      <Modal open={closeOpen} onOpenChange={setCloseOpen} title="Close finding">
        <div className="space-y-3">
          <Field label="Reason"><Select value={closure.reason} onChange={(v) => setClosure({ ...closure, reason: v })} options={["VERIFIED_FIXED", "RISK_ACCEPTED", "FALSE_POSITIVE", "DUPLICATE", "NOT_APPLICABLE"].map((v) => ({ value: v, label: humanize(v) }))} /></Field>
          <Field label="Rationale"><textarea className={inputCls} rows={3} value={closure.rationale} onChange={(e) => setClosure({ ...closure, rationale: e.target.value })} /></Field>
          <ErrorBox error={error} />
          <Button variant="primary" onClick={() => patch({ status: "CLOSED", closure })}>Close finding</Button>
        </div>
      </Modal>
      <EvidenceModal runId={x.run_id} executionId={evidence} onClose={() => setEvidence(null)} />
    </div>
  );
}

function RetestButton({ project, runId, ready }: { project: Project; runId: string; ready: number }) {
  const router = useRouter();
  const versions = useApi(() => api.get<SystemVersion[]>(`/v1/systems/${project.system_id}/versions`), [project.system_id]);
  const [open, setOpen] = useState(false);
  const [v, setV] = useState(project.candidate_version_id);
  const [scope, setScope] = useState("AFFECTED");
  const [error, setError] = useState<ApiError | null>(null);
  return (
    <>
      <Button variant="primary" disabled={!ready} onClick={() => setOpen(true)}>Retest {ready} ready finding{ready === 1 ? "" : "s"}</Button>
      <Modal open={open} onOpenChange={setOpen} title="Retest fixed findings" description="Reruns the affected cases (or the full suite) against the fixed build. Original evidence is kept unchanged.">
        <div className="space-y-3">
          <Field label="Build to test"><Select value={v} onChange={setV} options={(versions.data ?? []).map((x) => ({ value: x.id, label: x.version }))} /></Field>
          <Field label="Scope"><Select value={scope} onChange={setScope} options={[{ value: "AFFECTED", label: "Affected cases only" }, { value: "FULL", label: "Full suite" }]} /></Field>
          <ErrorBox error={error} />
          <Button variant="primary" onClick={async () => {
            setError(null);
            try { const r = await api.post<{ run_id: string }>(`/v1/runs/${runId}/retest`, { candidate_version_id: v, scope }); router.push(`/projects/${project.id}/runs/${r.run_id}`); }
            catch (e) { setError(e as ApiError); }
          }}>Queue retest</Button>
        </div>
      </Modal>
    </>
  );
}

function FindingsInner() {
  const { id } = useParams<{ id: string }>();
  const sp = useSearchParams();
  const { has } = useAuth();
  const project = useApi(() => api.get<Project>(`/v1/projects/${id}`), [id]);
  const runs = useApi(() => api.get<Run[]>(`/v1/runs?project_id=${id}`), [id]);
  const users = useApi(() => api.get<UserRef[]>("/v1/users"), []);
  const [runId, setRunId] = useState(sp.get("run") ?? "");
  const [status, setStatus] = useState("");
  const [severity, setSeverity] = useState("");
  const list = useApi(() => api.get<{ total: number; items: Finding[] }>(`/v1/findings${qs({ project_id: id, run_id: runId, status, severity, limit: 200 })}`), [id, runId, status, severity]);
  const [sel, setSel] = useState<string | null>(sp.get("f"));
  useEffect(() => { if (!runId && runs.data?.length) setRunId(runs.data.find((r) => r.status === "COMPLETED" && r.scope === "FULL")?.id ?? ""); }, [runs.data, runId]);
  useEffect(() => { if (!sel && list.data?.items.length) setSel(list.data.items[0].id); }, [list.data, sel]);

  const ready = (list.data?.items ?? []).filter((f) => f.status === "READY_FOR_RETEST").length;
  return (
    <>
      <PageHeader title="Findings investigation" subtitle="Step 6–7: investigate, assign, fix and retest. Related failures are grouped by root cause."
                  actions={project.data && runId && has(...CAN.createRun) ? <RetestButton project={project.data} runId={runId} ready={ready} /> : null} />
      <div className="mb-4 grid gap-3 md:grid-cols-4">
        <Select aria-label="Run" value={runId} onChange={(v) => { setRunId(v); setSel(null); }} placeholder="All runs"
                options={(runs.data ?? []).filter((r) => r.status === "COMPLETED").map((r) => ({ value: r.id, label: `Run #${r.run_number} · ${r.config.versions.candidate.version} · ${r.scope}` }))} />
        <Select aria-label="Status" value={status} onChange={setStatus} placeholder="Any status" options={Object.keys(TRANSITIONS).map((s) => ({ value: s, label: humanize(s) }))} />
        <Select aria-label="Severity" value={severity} onChange={setSeverity} placeholder="Any severity" options={["CRITICAL", "HIGH", "MEDIUM", "LOW"].map((s) => ({ value: s, label: s }))} />
        {runId && <Link href={`/projects/${id}/runs/${runId}`} className="self-center text-sm text-blue-800 hover:underline">Open run →</Link>}
      </div>
      <ErrorBox error={list.error} />
      <div className="grid gap-6 lg:grid-cols-5">
        <div className="lg:col-span-2">
          {list.loading ? <Loading /> : !list.data?.items.length ? <Empty>No findings match.</Empty> : (
            <ul className="divide-y divide-slate-200 overflow-hidden rounded-lg border border-slate-200 bg-white">
              {list.data.items.map((f) => (
                <li key={f.id}>
                  <button onClick={() => setSel(f.id)} aria-pressed={sel === f.id} className={`w-full px-3 py-2.5 text-left hover:bg-slate-50 ${sel === f.id ? "bg-blue-50" : ""}`}>
                    <div className="flex items-center gap-2"><span className="font-mono text-xs text-slate-500">{f.key}</span><Badge value={f.severity} /><Badge value={f.status} />
                      <span className="ml-auto text-xs tabular-nums text-slate-500">{f.affected_count} cases</span></div>
                    <div className="mt-1 text-sm text-slate-800">{f.title}</div>
                    <div className="text-xs text-slate-500">{f.owner ? `Owner ${f.owner.name}` : "Unassigned"}</div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="lg:col-span-3">{sel ? <Detail key={sel} findingId={sel} users={users.data ?? []} onChanged={list.reload} /> : <Empty>Select a finding.</Empty>}</div>
      </div>
    </>
  );
}

export default function FindingsPage() {
  return <Suspense fallback={<Loading />}><FindingsInner /></Suspense>;
}
