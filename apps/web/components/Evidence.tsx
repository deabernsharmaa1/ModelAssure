"use client";

import { api } from "@/lib/api";
import { fmtDate, short } from "@/lib/format";
import type { ExecutionDetail, ObservationEvidence } from "@/lib/types";
import { Badge, Decision, ErrorBox, Json, Loading, Modal, Mono, Table, Td, useApi } from "./ui";

function Side({ label, obs }: { label: string; obs: ObservationEvidence | null }) {
  if (!obs) return <div className="rounded-md border border-slate-200 p-3 text-sm text-slate-500">{label}: not called</div>;
  return (
    <div className="min-w-0 rounded-md border border-slate-200 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
        <b>{label}</b> <Badge value={obs.status === "OK" ? "PASS" : obs.status === "REJECTED" ? "SKIPPED" : "ERROR"} label={obs.status} />
        <span className="text-xs text-slate-500">HTTP {obs.http_status ?? "—"} · {obs.duration_ms ?? "—"} ms</span>
      </div>
      {obs.error_type && <p className="mb-2 text-sm text-fuchsia-800"><b>{obs.error_type}</b>: {obs.error_detail}</p>}
      <div className="mb-2 text-sm">Normalised decision: <Decision value={(obs.normalized?.decision as string) ?? null} />
        {Array.isArray(obs.normalized?.reason_codes) && <span className="ml-2 text-xs text-slate-600">{(obs.normalized!.reason_codes as string[]).join(", ")}</span>}</div>
      <details><summary className="cursor-pointer text-xs text-blue-800">Request sent</summary><Json value={obs.raw_request} /></details>
      <details><summary className="cursor-pointer text-xs text-blue-800">Raw response</summary><Json value={obs.raw_response} /></details>
      <details><summary className="cursor-pointer text-xs text-blue-800">Normalised output &amp; transformations</summary><Json value={{ normalized: obs.normalized, transformations: obs.transformations }} /></details>
    </div>
  );
}

export function EvidenceModal({ runId, executionId, onClose }: { runId: string; executionId: string | null; onClose: () => void }) {
  const ev = useApi(() => (executionId ? api.get<ExecutionDetail>(`/v1/runs/${runId}/executions/${executionId}`) : Promise.resolve(null)), [executionId]);
  const e = ev.data;
  return (
    <Modal open={!!executionId} onOpenChange={(o) => !o && onClose()} wide title={e ? `Case ${e.case_key}` : "Case evidence"}
           description="Reproducible evidence: re-send the same request to the same versions to reproduce this outcome.">
      {ev.loading ? <Loading /> : !e ? <ErrorBox error={ev.error} /> : (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <Badge value={e.result} /> {e.change_class && <Badge value={e.change_class} />}
            <span>{e.category}</span>
            <span>Baseline <Decision value={e.baseline_decision} /> → candidate <Decision value={e.candidate_decision} /></span>
            <span className={e.evidence_integrity.verified ? "text-green-700" : "text-red-700"}>
              {e.evidence_integrity.verified ? "✔ evidence integrity verified" : "✖ evidence hash mismatch"} <Mono>{short(e.evidence_integrity.sha256, 12)}</Mono>
            </span>
          </div>
          <div className="text-xs text-slate-500">Trace <Mono>{e.evidence.trace_id ?? "—"}</Mono> · executed {fmtDate(e.evidence.executed_at)} · schemas {Object.values(e.evidence.schema_versions ?? {}).join(", ")}</div>
          {e.summary?.violations?.length ? (
            <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm">
              {e.summary.violations.map((v, i) => (
                <div key={i}><b>{v.policy_id ?? v.failure_type}</b> <Badge value={v.severity} />: expected <b>{String(v.expected)}</b>, observed <b>{String(v.observed)}</b>{v.reason ? ` — ${v.reason}` : ""}</div>
              ))}
            </div>
          ) : null}
          {(e.detail.inconclusive_reasons as string[] | undefined)?.length ? <p className="text-sm text-amber-800">{(e.detail.inconclusive_reasons as string[]).join("; ")}</p> : null}
          {(e.detail.explained_by as string[] | undefined)?.length ? <p className="text-sm text-sky-800">Change explained by: {(e.detail.explained_by as string[]).join(", ")}</p> : null}
          <div className="grid gap-3 lg:grid-cols-3">
            <div className="min-w-0 rounded-md border border-slate-200 p-3">
              <b className="text-sm">Test input</b>
              <div className="text-xs text-slate-600">Injected faults: {Object.entries((e.evidence.scenario?.faults as Record<string, string>) ?? {}).filter(([, v]) => v !== "NONE").map(([k, v]) => `${k}=${v}`).join(", ") || "none"}</div>
              <Json value={e.evidence.input} />
            </div>
            <Side label="Baseline" obs={e.evidence.baseline} />
            <Side label="Candidate" obs={e.evidence.candidate} />
          </div>
          {e.detail.policy_outcomes?.length ? (
            <Table head={["Policy", "Outcome", "Requires", "Observed", "Note"]}>
              {e.detail.policy_outcomes.map((o) => (
                <tr key={o.policy_id}><Td><Mono>{o.policy_id}</Mono></Td>
                  <Td><Badge value={o.status === "VIOLATED" ? "FAIL" : o.status === "SATISFIED" ? "PASS" : o.status === "UNKNOWN" ? "INCONCLUSIVE" : "SKIPPED"} label={o.status.replace("_", " ")} /></Td>
                  <Td className="text-xs">{o.expected}</Td><Td><Decision value={String(o.observed ?? "")} /></Td><Td className="text-xs text-slate-600">{o.status === "NOT_APPLICABLE" ? "" : o.reason}</Td></tr>
              ))}
            </Table>
          ) : null}
        </div>
      )}
    </Modal>
  );
}
