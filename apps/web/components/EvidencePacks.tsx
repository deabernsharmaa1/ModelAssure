"use client";

import { useState } from "react";
import { ApiError, api, download } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtDate, short } from "@/lib/format";
import type { ArtifactRow } from "@/lib/types";
import { Badge, Button, Card, Empty, ErrorBox, Field, Mono, Select, useApi } from "./ui";

interface SigningKeyRow { fingerprint: string; status: string; created_at: string; retired_at: string | null }

const CASE_OPTIONS = [
  { value: "none", label: "No case-level evidence (outcomes only)" },
  { value: "failing", label: "Raw evidence for failing, error and inconclusive cases" },
  { value: "all", label: "Raw evidence for every case" },
];

export function EvidencePacks({ runId, runNumber, completed, onCreated }: { runId: string; runNumber: number; completed: boolean; onCreated: () => void }) {
  const { has } = useAuth();
  const packs = useApi(() => api.get<ArtifactRow[]>(`/v1/runs/${runId}/evidence-packs`), [runId]);
  const keys = useApi(() => api.get<SigningKeyRow[]>("/v1/signing-keys"), [runId]);
  const [mode, setMode] = useState("none");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const can = has(...CAN.evidencePack);
  const active = keys.data?.find((k) => k.status === "ACTIVE");

  const create = async () => {
    setBusy(true); setError(null);
    try {
      await api.post(`/v1/runs/${runId}/evidence-packs`, { case_evidence: mode });
      await Promise.all([packs.reload(), keys.reload()]);
      onCreated();
    } catch (e) { setError(e as ApiError); } finally { setBusy(false); }
  };
  return (
    <Card title="Signed evidence pack">
      <p className="mb-3 text-xs text-slate-600">
        One zip for a bank, auditor or supervisor: report, outcomes, configuration (secrets removed), findings, reviews,
        regulatory mapping and audit events, with a signed manifest and an offline verifier. It proves the files are
        unaltered and came from this workspace. It is not a certification.
      </p>
      {active ? (
        <p className="mb-3 text-xs text-slate-700">Signing key fingerprint: <Mono>{short(active.fingerprint, 16)}</Mono>{" "}
          <button className="text-blue-800 underline" onClick={() => navigator.clipboard?.writeText(active.fingerprint)}>copy</button>
          <span className="block text-slate-500">Send the full fingerprint to recipients separately from the pack.</span></p>
      ) : <p className="mb-3 text-xs text-slate-500">A signing key is created with the first pack.</p>}
      {can ? (
        <div className="space-y-2">
          <Field label="Case-level evidence" hint={mode !== "none" ? "Raw requests and responses can contain applicant data. Share only where your data-processing terms allow." : undefined}>
            <Select value={mode} onChange={setMode} options={CASE_OPTIONS} />
          </Field>
          <ErrorBox error={error} />
          <Button size="sm" variant="primary" onClick={create} disabled={busy || !completed}>{busy ? "Signing…" : "Create signed pack"}</Button>
        </div>
      ) : <p className="text-xs text-slate-600">Risk managers, reviewers, auditors and admins can issue evidence packs.</p>}
      <div className="mt-3 border-t border-slate-200 pt-3">
        {!packs.data?.length ? <Empty>No evidence packs yet.</Empty> : (
          <ul className="space-y-2 text-sm">
            {packs.data.map((p) => (
              <li key={p.id} className="rounded-md border border-slate-200 p-2">
                <div className="flex items-center justify-between"><b>Pack v{p.version}</b><span className="text-xs text-slate-500">{fmtDate(p.created_at)}</span></div>
                <div className="text-xs text-slate-600">
                  <Badge value={String(p.metadata.assessment)} /> · report v{String(p.metadata.report_version)} · {(p.size_bytes / 1024).toFixed(0)} KB ·
                  key <Mono>{short(String(p.metadata.key_fingerprint), 10)}</Mono> · cases {String((p.metadata.case_evidence as { mode?: string })?.mode ?? "none")}
                </div>
                {can && <div className="mt-2"><Button size="sm" onClick={() => download(`/v1/runs/${runId}/evidence-packs/${p.version}`, `modelassure-run-${runNumber}-evidence-pack-v${p.version}.zip`).catch((e) => setError(e))}>Download</Button></div>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
