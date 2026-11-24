"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { EvidenceModal } from "@/components/Evidence";
import { Badge, Button, Card, Decision, Empty, ErrorBox, Loading, Mono, OutcomeBanner, PageHeader, Progress, Select, Stat, Table, Td, useApi } from "@/components/ui";
import { api, download, qs } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { counts, fmtDate, myr, pct, short } from "@/lib/format";
import type { Execution, Run } from "@/lib/types";

const DECISIONS = ["APPROVE", "REFER", "REVIEW", "HOLD", "DECLINE"];
const PAGE = 50;

export default function RunPage() {
  const { id, runId } = useParams<{ id: string; runId: string }>();
  const { has } = useAuth();
  const run = useApi(() => api.get<Run>(`/v1/runs/${runId}`), [runId], (d) => (d && ["QUEUED", "RUNNING", "CANCELLING"].includes(d.status) ? 1500 : null));
  const [filter, setFilter] = useState({ result: "", category: "", change_class: "", rule: "" });
  const [offset, setOffset] = useState(0);
  const execs = useApi(() => api.get<{ total: number; items: Execution[] }>(`/v1/runs/${runId}/executions${qs({ ...filter, limit: PAGE, offset })}`),
                       [runId, filter.result, filter.category, filter.change_class, filter.rule, offset, run.data?.status]);
  const [open, setOpen] = useState<string | null>(null);

  if (run.loading) return <Loading />;
  if (!run.data) return <ErrorBox error={run.error} />;
  const r = run.data;
  const s = r.summary;
  const live = ["QUEUED", "RUNNING", "CANCELLING"].includes(r.status);
  const setF = (k: keyof typeof filter) => (v: string) => { setOffset(0); setFilter((f) => ({ ...f, [k]: v })); };

  return (
    <>
      <PageHeader title={<>Run #{r.run_number} <Badge value={r.status} /></>}
                  crumbs={<Link href={`/projects/${id}/runs`} className="hover:underline">Test execution</Link>}
                  subtitle={<>Baseline <b>{r.config.versions.baseline.version}</b> → candidate <b>{r.config.versions.candidate.version}</b> · {r.config.suite.name} v{r.config.suite.version} ·
                    policy {r.config.policy_sets.candidate.label} ({r.config.policy_sets.candidate.approval_status}) · {r.scope}{r.parent_run_id ? " retest" : ""}</>}
                  actions={<>
                    {live && has(...CAN.createRun) && <Button onClick={async () => { await api.post(`/v1/runs/${runId}/cancel`); await run.reload(); }}>Cancel</Button>}
                    {!live && <Link href={`/projects/${id}/findings?run=${runId}`} className="rounded-md px-3 py-1.5 text-sm ring-1 ring-slate-300 hover:bg-slate-50">Findings</Link>}
                    {!live && <Link href={`/projects/${id}/release?run=${runId}`} className="rounded-md bg-[#0b2545] px-3 py-1.5 text-sm font-medium text-white">Release review</Link>}
                  </>} />
      {live && (
        <Card className="mb-6"><div className="space-y-2"><Progress percent={r.progress.percent} />
          <div className="text-sm text-slate-600">{r.progress.completed.toLocaleString()} of {r.progress.total.toLocaleString()} cases executed ({r.progress.percent}%)</div></div></Card>
      )}
      {r.failure_reason && <div className="mb-4"><ErrorBox error={{ message: `Run failed: ${r.failure_reason}` }} /></div>}
      {r.assessment && (
        <div className="mb-6 space-y-2">
          <OutcomeBanner outcome={r.assessment.outcome} text={r.assessment.recommendation} />
          {[...r.assessment.fail_reasons, ...r.assessment.inconclusive_reasons].length > 0 && (
            <ul className="list-disc pl-6 text-sm text-slate-700">{[...r.assessment.fail_reasons, ...r.assessment.inconclusive_reasons].map((x) => <li key={x}>{x}</li>)}</ul>
          )}
          {r.assessment.flags.map((x) => <p key={x} className="text-sm text-amber-800">⚑ {x}</p>)}
        </div>
      )}
      {s && (
        <>
          <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-6">
            <Stat label="Executed" value={s.total.toLocaleString()} />
            <Stat label="Failed" value={s.by_result.FAIL} tone={s.by_result.FAIL ? "red" : "green"} />
            <Stat label="Changed" value={s.by_result.CHANGED} tone="blue" hint={`${s.changes.expected} expected · ${s.changes.unexpected} unexpected`} />
            <Stat label="Technical errors" value={s.by_result.ERROR} tone={s.by_result.ERROR ? "amber" : "slate"} />
            <Stat label="Inconclusive" value={s.by_result.INCONCLUSIVE} tone={s.by_result.INCONCLUSIVE ? "amber" : "slate"} />
            <Stat label="Decision agreement" value={pct(s.decision_agreement_rate)} hint={s.agreement_note} />
          </div>
          <div className="mb-6 grid gap-6 lg:grid-cols-2">
            <Card title="Coverage by scenario category" pad={false}>
              <Table head={["Category", "Total", "Pass", "Fail", "Changed", "Error", "Inconcl.", "Skipped"]}>
                {s.coverage.map((c) => (
                  <tr key={c.category} className="tabular-nums">
                    <Td className="text-xs">{c.category.replace(/_/g, " ").toLowerCase()}</Td><Td>{c.total}</Td><Td>{c.pass}</Td>
                    <Td className={c.fail ? "font-semibold text-red-700" : ""}>{c.fail}</Td><Td>{c.changed}</Td><Td>{c.error}</Td><Td>{c.inconclusive}</Td><Td>{c.skipped}</Td>
                  </tr>
                ))}
              </Table>
            </Card>
            <Card title="Decision transitions (baseline → candidate)" pad={false}>
              <Table head={["Baseline ↓ / Candidate →", ...DECISIONS]}>
                {DECISIONS.map((b) => (
                  <tr key={b} className="tabular-nums"><Td><Decision value={b} /></Td>
                    {DECISIONS.map((c) => { const n = s.transition_matrix[b]?.[c]; return <Td key={c} className={n && b !== c ? "bg-amber-50 font-semibold" : ""}>{n ?? ""}</Td>; })}</tr>
                ))}
              </Table>
              <div className="border-t border-slate-200 px-4 py-2 text-xs text-slate-600">
                Changes: {s.changes.expected} expected · {s.changes.unexpected} unexpected · {s.changes.prohibited} prohibited · {s.changes.with_violation} with a violation.
                Newly auto-approved: {myr(s.exposure.newly_approved_amount)} · no longer auto-approved: {myr(s.exposure.no_longer_approved_amount)} <span className="text-slate-400">(synthetic exposure, not a loss estimate)</span>
              </div>
            </Card>
          </div>
          {s.dependency_behaviour.length > 0 && (
            <Card title="Behaviour under injected dependency failures" pad={false} className="mb-6">
              <Table head={["Fault", "Cases", "Baseline decisions", "Candidate decisions", "Failed"]}>
                {s.dependency_behaviour.map((d) => (
                  <tr key={d.fault}><Td><Mono>{d.fault}</Mono></Td><Td>{d.cases}</Td><Td className="text-xs">{counts(d.baseline_decisions)}</Td>
                    <Td className="text-xs">{counts(d.candidate_decisions)}</Td><Td className={d.failed ? "font-semibold text-red-700" : ""}>{d.failed}</Td></tr>
                ))}
              </Table>
            </Card>
          )}
        </>
      )}

      <Card title="Executions" pad={false}
            actions={!live ? <Button size="sm" onClick={() => download(`/v1/runs/${runId}/executions.csv`, `run-${r.run_number}-outcomes.csv`)}>Export CSV</Button> : null}>
        <div className="grid gap-3 border-b border-slate-200 p-3 md:grid-cols-4">
          <Select aria-label="Result" value={filter.result} onChange={setF("result")} placeholder="All results" options={["FAIL", "CHANGED", "ERROR", "INCONCLUSIVE", "PASS", "SKIPPED"].map((v) => ({ value: v, label: v }))} />
          <Select aria-label="Category" value={filter.category} onChange={setF("category")} placeholder="All categories" options={(s?.coverage ?? []).map((c) => ({ value: c.category, label: c.category }))} />
          <Select aria-label="Change class" value={filter.change_class} onChange={setF("change_class")} placeholder="Any change class" options={["EXPECTED", "UNEXPECTED", "PROHIBITED", "VIOLATION"].map((v) => ({ value: v, label: v }))} />
          <Select aria-label="Violated rule" value={filter.rule} onChange={setF("rule")} placeholder="Any rule" options={Object.keys(s?.violations_by_rule ?? {}).map((v) => ({ value: v, label: `${v} (${s!.violations_by_rule[v]})` }))} />
        </div>
        {execs.loading && !execs.data ? <Loading /> : (
          <Table head={["Case", "Category", "Result", "Baseline", "Candidate", "Change", "Rules / error", ""]}
                 empty={!execs.data?.items.length && <div className="p-4"><Empty>No executions match.</Empty></div>}>
            {execs.data?.items.map((e) => (
              <tr key={e.id} className="hover:bg-slate-50">
                <Td><Mono>{e.case_key}</Mono></Td><Td className="text-xs">{e.category.replace(/_/g, " ").toLowerCase()}</Td>
                <Td><Badge value={e.result} /></Td><Td><Decision value={e.baseline_decision} /></Td><Td><Decision value={e.candidate_decision} /></Td>
                <Td><Badge value={e.change_class} /></Td>
                <Td className="text-xs">{e.violated_rules.join(", ") || e.error_type || e.summary?.explained_by?.join(", ") || ""}</Td>
                <Td><Button size="sm" variant="ghost" onClick={() => setOpen(e.id)}>Evidence</Button></Td>
              </tr>
            ))}
          </Table>
        )}
        {execs.data && execs.data.total > PAGE && (
          <div className="flex items-center justify-between border-t border-slate-200 px-3 py-2 text-xs text-slate-600">
            <span>{offset + 1}–{Math.min(offset + PAGE, execs.data.total)} of {execs.data.total}</span>
            <div className="flex gap-2"><Button size="sm" disabled={offset === 0} onClick={() => setOffset(offset - PAGE)}>Previous</Button>
              <Button size="sm" disabled={offset + PAGE >= execs.data.total} onClick={() => setOffset(offset + PAGE)}>Next</Button></div>
          </div>
        )}
      </Card>
      <p className="mt-3 text-xs text-slate-500">Configuration hash <Mono>{short(r.snapshot_hash, 16)}</Mono> · started {fmtDate(r.started_at)} · completed {fmtDate(r.completed_at)}</p>
      <EvidenceModal runId={runId} executionId={open} onClose={() => setOpen(null)} />
    </>
  );
}
