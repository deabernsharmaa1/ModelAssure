"use client";

import { api } from "@/lib/api";
import type { ComplianceResult } from "@/lib/types";
import { Badge, Card, ErrorBox, Loading, Tabs, useApi } from "./ui";

const ORDER = ["NOT_MET", "NO_EVIDENCE", "PARTIAL", "MET", "OUT_OF_SCOPE"];

export function RegulatoryMapping({ runId }: { runId: string }) {
  const m = useApi(() => api.get<ComplianceResult>(`/v1/runs/${runId}/compliance`), [runId]);
  if (m.loading) return <Card title="Regulatory mapping"><Loading /></Card>;
  if (!m.data) return <ErrorBox error={m.error} />;
  return (
    <Card title="Regulatory mapping">
      <p className="mb-3 text-xs text-slate-600">{m.data.disclaimer}</p>
      <Tabs tabs={m.data.frameworks.map((f) => ({
        value: f.framework_id,
        label: <span className="whitespace-nowrap">{f.framework_id.replace(/-\d{4}$/, "").replace(/-/g, " ")}</span>,
        content: (
          <div>
            <div className="mb-2 text-sm"><b>{f.name}</b> <span className="text-slate-500">· {f.issuer} · {f.status.replace(/_/g, " ").toLowerCase()}</span></div>
            <p className="mb-2 text-xs text-slate-600">{f.nature}{f.reference_note ? ` ${f.reference_note}` : ""} <a className="text-blue-800 underline" href={f.source_url} target="_blank" rel="noreferrer">Source</a></p>
            <div className="mb-3 flex flex-wrap gap-2">{ORDER.filter((s) => f.summary[s]).map((s) => <Badge key={s} value={s} label={`${s.replace(/_/g, " ")} ${f.summary[s]}`} />)}</div>
            {!f.controls.length ? <p className="text-sm text-slate-600">Controls will be mapped when this framework is published.</p> : (
              <ul className="divide-y divide-slate-100">
                {f.controls.map((c) => (
                  <li key={c.control_id} className="py-2">
                    <details>
                      <summary className="flex cursor-pointer list-none items-start gap-3">
                        <span className="w-24 shrink-0 font-mono text-xs text-slate-600">{c.reference}</span>
                        <span className="w-28 shrink-0"><Badge value={c.status} /></span>
                        <span className="text-sm"><b>{c.topic}.</b> <span className="text-slate-600">{c.expectation}</span></span>
                      </summary>
                      <div className="ml-[13.5rem] mt-2 space-y-1 text-xs">
                        {c.checks.map((x) => <div key={x.check}><Badge value={x.status} /> <span className="text-slate-700">{x.detail}</span></div>)}
                        {c.note && <div className="text-slate-500">{c.note}</div>}
                      </div>
                    </details>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ),
      }))} />
    </Card>
  );
}
