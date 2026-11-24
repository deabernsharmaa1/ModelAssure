"use client";

import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { RegulatoryMapping } from "@/components/RegulatoryMapping";
import { Badge, Button, Card, Empty, ErrorBox, Field, Loading, Mono, OutcomeBanner, PageHeader, Select, Table, Td, inputCls, useApi } from "@/components/ui";
import { ApiError, api, download } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtDate, short } from "@/lib/format";
import type { ArtifactRow, Assessment, Finding, Review, Run } from "@/lib/types";

const UNRESOLVED = ["OPEN", "INVESTIGATING", "FIX_PROPOSED", "READY_FOR_RETEST"];

function ReleaseInner() {
  const { id } = useParams<{ id: string }>();
  const sp = useSearchParams();
  const { has } = useAuth();
  const runs = useApi(() => api.get<Run[]>(`/v1/runs?project_id=${id}`), [id]);
  const [runId, setRunId] = useState(sp.get("run") ?? "");
  useEffect(() => {
    if (!runId && runs.data?.length) setRunId(runs.data.find((r) => r.status === "COMPLETED" && r.scope === "FULL")?.id ?? runs.data[0].id);
  }, [runs.data, runId]);
  const run = runs.data?.find((r) => r.id === runId);
  const assessment = useApi(() => (runId ? api.get<Assessment>(`/v1/runs/${runId}/assessment`) : Promise.resolve(null)), [runId]);
  const findings = useApi(() => (runId ? api.get<{ items: Finding[] }>(`/v1/findings?run_id=${runId}&limit=200`) : Promise.resolve({ items: [] })), [runId]);
  const reviews = useApi(() => (runId ? api.get<Review[]>(`/v1/runs/${runId}/reviews`) : Promise.resolve([])), [runId]);
  const reports = useApi(() => (runId ? api.get<ArtifactRow[]>(`/v1/runs/${runId}/reports`) : Promise.resolve([])), [runId]);
  const [decision, setDecision] = useState("APPROVE_RELEASE");
  const [rationale, setRationale] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  const a = assessment.data;
  const outstanding = (findings.data?.items ?? []).filter((f) => UNRESOLVED.includes(f.status));
  const accepted = (findings.data?.items ?? []).filter((f) => f.status === "ACCEPTED_RISK");
  const versions = new Map<number, ArtifactRow[]>();
  for (const r of reports.data ?? []) versions.set(r.version, [...(versions.get(r.version) ?? []), r]);

  const submit = async () => {
    setError(null); setBusy(true);
    try { await api.post(`/v1/runs/${runId}/reviews`, { decision, rationale }); setRationale(""); await reviews.reload(); await runs.reload(); }
    catch (e) { setError(e as ApiError); } finally { setBusy(false); }
  };
  const generate = async () => {
    setError(null); setBusy(true);
    try { await api.post(`/v1/runs/${runId}/report`); await reports.reload(); } catch (e) { setError(e as ApiError); } finally { setBusy(false); }
  };

  return (
    <>
      <PageHeader title="Release review & reports" subtitle="Step 8: the evidence goes to authorised human reviewers. The institution retains final release authority." />
      <div className="mb-4 max-w-md">
        <Select aria-label="Run" value={runId} onChange={setRunId}
                options={(runs.data ?? []).filter((r) => r.status === "COMPLETED").map((r) => ({ value: r.id, label: `Run #${r.run_number} · ${r.config.versions.baseline.version} → ${r.config.versions.candidate.version} · ${r.scope} · ${r.assessment?.outcome ?? ""}` }))} />
      </div>
      {!run ? (runs.loading ? <Loading /> : <Empty>No completed runs yet.</Empty>) : (
        <div className="grid gap-6 lg:grid-cols-3">
          <div className="space-y-6 lg:col-span-2">
            <Card title={<>Release assessment — run <Link href={`/projects/${id}/runs/${run.id}`} className="text-blue-800 hover:underline">#{run.run_number}</Link></>}>
              {!a ? <Loading /> : (
                <div className="space-y-3">
                  <OutcomeBanner outcome={a.outcome} text={a.recommendation} />
                  {a.fail_reasons.length > 0 && <div><div className="text-xs font-semibold uppercase text-red-800">Fails the release criteria</div><ul className="list-disc pl-5 text-sm">{a.fail_reasons.map((x) => <li key={x}>{x}</li>)}</ul></div>}
                  {a.inconclusive_reasons.length > 0 && <div><div className="text-xs font-semibold uppercase text-amber-800">Evidence gaps</div><ul className="list-disc pl-5 text-sm">{a.inconclusive_reasons.map((x) => <li key={x}>{x}</li>)}</ul></div>}
                  {a.flags.length > 0 && <div><div className="text-xs font-semibold uppercase text-slate-600">Flagged for review</div><ul className="list-disc pl-5 text-sm">{a.flags.map((x) => <li key={x}>{x}</li>)}</ul></div>}
                  <p className="rounded bg-slate-50 px-3 py-2 text-xs text-slate-600">{a.disclaimer}</p>
                </div>
              )}
            </Card>

            <RegulatoryMapping runId={run.id} />

            <Card title={`Outstanding findings (${outstanding.length})`} pad={false}>
              <Table head={["Finding", "Severity", "Status", "Owner", "Cases"]} empty={!outstanding.length && <div className="p-4"><Empty>No unresolved findings.</Empty></div>}>
                {outstanding.map((f) => (
                  <tr key={f.id}><Td><Link href={`/projects/${id}/findings?run=${run.id}&f=${f.id}`} className="text-blue-800 hover:underline">{f.key}</Link><div className="max-w-md text-xs text-slate-600">{f.title}</div></Td>
                    <Td><Badge value={f.severity} /></Td><Td><Badge value={f.status} /></Td><Td className="text-xs">{f.owner?.name ?? "—"}</Td><Td className="tabular-nums">{f.affected_count}</Td></tr>
                ))}
              </Table>
            </Card>

            {accepted.length > 0 && (
              <Card title="Documented risk acceptance" pad={false}>
                <Table head={["Finding", "Owner", "Authorised by", "Rationale"]}>
                  {accepted.map((f) => (
                    <tr key={f.id}><Td>{f.key} <Badge value={f.severity} /></Td><Td className="text-xs">{f.risk_acceptance?.owner_email}</Td>
                      <Td className="text-xs">{f.risk_acceptance?.authorised_by}<div className="text-slate-500">{fmtDate(f.risk_acceptance?.authorised_at)}</div></Td><Td className="text-xs">{f.risk_acceptance?.rationale}</Td></tr>
                  ))}
                </Table>
              </Card>
            )}

            <Card title="Reviewer decisions" pad={false}>
              <Table head={["When", "Reviewer", "Decision", "Assessment at review", "Rationale"]}
                     empty={!reviews.data?.length && <div className="p-4"><Empty>No decision recorded for this run.</Empty></div>}>
                {reviews.data?.map((r) => (
                  <tr key={r.id}><Td className="text-xs">{fmtDate(r.created_at)}</Td><Td className="text-xs">{r.reviewer?.name}<div className="text-slate-500">{r.reviewer?.email}</div></Td>
                    <Td><Badge value={r.decision === "APPROVE_RELEASE" ? "PASS" : r.decision === "REJECT_RELEASE" ? "FAIL" : "INCONCLUSIVE"} label={r.decision.replace(/_/g, " ")} /></Td>
                    <Td><Badge value={r.assessment_snapshot.outcome} /></Td><Td className="text-xs">{r.rationale}</Td></tr>
                ))}
              </Table>
            </Card>
          </div>

          <div className="space-y-6">
            <Card title="Record release decision">
              {has(...CAN.review) ? (
                <div className="space-y-3">
                  <Field label="Decision"><Select value={decision} onChange={setDecision} options={[
                    { value: "APPROVE_RELEASE", label: "Approve release" }, { value: "REQUEST_REMEDIATION", label: "Request remediation" }, { value: "REJECT_RELEASE", label: "Reject release" }]} /></Field>
                  <Field label="Rationale"><textarea className={inputCls} rows={4} value={rationale} onChange={(e) => setRationale(e.target.value)} /></Field>
                  <ErrorBox error={error} />
                  <Button variant="primary" onClick={submit} disabled={busy || rationale.length < 10 || run.scope !== "FULL" && decision === "APPROVE_RELEASE"}>Record decision</Button>
                  <p className="text-xs text-slate-500">Approval is only possible when the assessment is PASS. People who built or configured the candidate, or started the run, cannot review it. ModelAssure records the decision; it does not deploy.</p>
                </div>
              ) : <p className="text-sm text-slate-600">Only users with the Reviewer role can record release decisions.</p>}
            </Card>

            <Card title="Reports" actions={<Button size="sm" variant="primary" onClick={generate} disabled={busy}>Generate new version</Button>}>
              {!versions.size ? <Empty>No report generated yet.</Empty> : (
                <ul className="space-y-3 text-sm">
                  {[...versions.entries()].sort((x, y) => y[0] - x[0]).map(([v, arts]) => (
                    <li key={v} className="rounded-md border border-slate-200 p-2">
                      <div className="flex items-center justify-between"><b>Version {v}</b><span className="text-xs text-slate-500">{fmtDate(arts[0].created_at)}</span></div>
                      <div className="text-xs text-slate-600">Assessment {String(arts[0].metadata.assessment)} · results digest <Mono>{short(String(arts[0].metadata.results_digest), 12)}</Mono></div>
                      <div className="mt-2 flex gap-2">
                        {arts.map((art) => (
                          <Button key={art.id} size="sm" onClick={() => download(`/v1/runs/${run.id}/report?format=${art.type === "REPORT_PDF" ? "pdf" : "json"}&version=${v}`, `modelassure-run-${run.run_number}-v${v}.${art.type === "REPORT_PDF" ? "pdf" : "json"}`)}>
                            {art.type === "REPORT_PDF" ? "PDF" : "JSON"}
                          </Button>
                        ))}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-3 border-t border-slate-200 pt-3">
                <Button size="sm" onClick={() => download(`/v1/runs/${run.id}/executions.csv`, `run-${run.run_number}-outcomes.csv`)}>Export outcomes CSV</Button>
                <p className="mt-2 text-xs text-slate-500">Reports are sensitive. Every download is audit-logged and checksum-verified. Regenerating a report never changes the recorded results.</p>
              </div>
            </Card>
          </div>
        </div>
      )}
    </>
  );
}

export default function ReleasePage() {
  return <Suspense fallback={<Loading />}><ReleaseInner /></Suspense>;
}
