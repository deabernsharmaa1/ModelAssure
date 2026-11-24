"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Field, Loading, Modal, Mono, PageHeader, Select, inputCls, useApi } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtDate, short } from "@/lib/format";
import type { Connection, Project, SystemVersion } from "@/lib/types";

const EXAMPLE_MAPPING = {
  request: { fields: { applicationId: "case_id", productCode: "product", requestedAmount: "requested_amount", currency: "currency",
    monthlyCashAvailable: "monthly_cash_available", monthlyDebtService: "monthly_debt_service", "bureau.status": "bureau.status",
    "bureau.score": "bureau.score", "fraud.status": "fraud.status", "fraud.alertResolved": "fraud.alert_resolved", "identity.status": "identity.status" } },
  response: { fields: { case_id: { source: "applicationId" },
    decision: { source: "finalStatus", values: { APPROVED: "APPROVE", DECLINED: "DECLINE", MANUAL_REVIEW: "REVIEW", ON_HOLD: "HOLD", REFERRED: "REFER" } },
    pd: { source: "probabilityOfDefault" }, model_version: { source: "modelVersion" }, policy_version: { source: "policyVersion" },
    reason_codes: { source: "reasonCodes" }, trace_id: { source: "traceId" } } },
};

function NewConnection({ versions, onCreated }: { versions: SystemVersion[]; onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ system_version_id: "", endpoint: "", auth_header: "X-API-Key", credential: "", timeout_ms: "5000", max_concurrency: "5", max_rps: "", dependency_control_url: "", dependency_credential: "" });
  const [mapping, setMapping] = useState(JSON.stringify(EXAMPLE_MAPPING, null, 2));
  const [error, setError] = useState<ApiError | null>(null);
  const set = (k: keyof typeof f) => (v: string) => setF((x) => ({ ...x, [k]: v }));
  const submit = async () => {
    setError(null);
    let parsed: unknown;
    try { parsed = JSON.parse(mapping || "{}"); } catch { setError(new ApiError(0, "INVALID", "Mapping is not valid JSON")); return; }
    try {
      await api.post("/v1/connections", {
        system_version_id: f.system_version_id, kind: "REST", environment: "SANDBOX", endpoint: f.endpoint,
        auth_header: f.auth_header || null, credential: f.credential || null, mapping: parsed,
        timeout_ms: Number(f.timeout_ms), max_concurrency: Number(f.max_concurrency), max_rps: f.max_rps ? Number(f.max_rps) : null,
        dependency_control_url: f.dependency_control_url || null, dependency_credential: f.dependency_credential || null,
      });
      setOpen(false);
      onCreated();
    } catch (e) { setError(e as ApiError); }
  };
  return (
    <>
      <Button variant="primary" onClick={() => setOpen(true)}>Add connection</Button>
      <Modal open={open} onOpenChange={setOpen} wide title="Add an authorised sandbox connection"
             description="Only approved test destinations are accepted. Credentials go to the secret store and are never shown again.">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="System version"><Select value={f.system_version_id} onChange={set("system_version_id")} options={versions.map((v) => ({ value: v.id, label: v.version }))} placeholder="Select…" /></Field>
          <Field label="Decision endpoint (sandbox)"><input className={inputCls} value={f.endpoint} onChange={(e) => set("endpoint")(e.target.value)} placeholder="https://sandbox.vendor.example/v3.2/decisions" /></Field>
          <Field label="Auth header"><input className={inputCls} value={f.auth_header} onChange={(e) => set("auth_header")(e.target.value)} /></Field>
          <Field label="Credential" hint="Write-only"><input className={inputCls} type="password" autoComplete="off" value={f.credential} onChange={(e) => set("credential")(e.target.value)} /></Field>
          <Field label="Timeout (ms)"><input className={inputCls} type="number" value={f.timeout_ms} onChange={(e) => set("timeout_ms")(e.target.value)} /></Field>
          <Field label="Max concurrency" hint="Default 5 (NFR-03)"><input className={inputCls} type="number" value={f.max_concurrency} onChange={(e) => set("max_concurrency")(e.target.value)} /></Field>
          <Field label="Rate limit (requests/second)" hint="Optional; agree with the system owner"><input className={inputCls} type="number" value={f.max_rps} onChange={(e) => set("max_rps")(e.target.value)} /></Field>
          <Field label="Dependency simulator URL" hint="Needed for injected bureau/fraud/identity faults"><input className={inputCls} value={f.dependency_control_url} onChange={(e) => set("dependency_control_url")(e.target.value)} /></Field>
          <Field label="Dependency simulator key"><input className={inputCls} type="password" autoComplete="off" value={f.dependency_credential} onChange={(e) => set("dependency_credential")(e.target.value)} /></Field>
          <div className="md:col-span-2">
            <Field label="Field mapping (customer ↔ common decision schema)" hint="request.fields: customer field ← canonical field. response.fields: canonical field ← customer field, with decision value mapping.">
              <textarea className={`${inputCls} font-mono text-xs`} rows={12} value={mapping} onChange={(e) => setMapping(e.target.value)} spellCheck={false} />
            </Field>
          </div>
        </div>
        <div className="mt-4 space-y-3"><ErrorBox error={error} />
          <Button variant="primary" onClick={submit} disabled={!f.system_version_id || !f.endpoint}>Save connection</Button></div>
      </Modal>
    </>
  );
}

export default function ConnectionsPage() {
  const { id } = useParams<{ id: string }>();
  const { has } = useAuth();
  const project = useApi(() => api.get<Project>(`/v1/projects/${id}`), [id]);
  const versions = useApi(() => (project.data ? api.get<SystemVersion[]>(`/v1/systems/${project.data.system_id}/versions`) : Promise.resolve([])), [project.data?.system_id]);
  const conns = useApi(() => api.get<Connection[]>("/v1/connections"), []);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [mappingOf, setMappingOf] = useState<Connection | null>(null);

  const verify = async (c: Connection) => {
    setBusy(c.id); setError(null);
    try { await api.post(`/v1/connections/${c.id}/verify`); await conns.reload(); } catch (e) { setError(e as ApiError); } finally { setBusy(null); }
  };
  if (project.loading || versions.loading) return <Loading />;
  const vids = new Set((versions.data ?? []).map((v) => v.id));
  const mine = (conns.data ?? []).filter((c) => vids.has(c.system_version_id));
  const vName = (vid: string) => versions.data?.find((v) => v.id === vid)?.version ?? "?";

  return (
    <>
      <PageHeader title="Connections" subtitle="Step 2: connect the baseline and candidate decision systems. A connection must pass verification before it can be tested."
                  actions={has(...CAN.manageSystems) ? <NewConnection versions={versions.data ?? []} onCreated={conns.reload} /> : null} />
      <ErrorBox error={error ?? conns.error} />
      {!mine.length && <Empty>No connections yet.</Empty>}
      <div className="space-y-4">
        {(versions.data ?? []).map((v) => {
          const list = mine.filter((c) => c.system_version_id === v.id);
          if (!list.length) return null;
          return (
            <Card key={v.id} title={<>Version {v.version} {v.id === project.data?.candidate_version_id ? <Badge value="RUNNING" label="candidate" /> : v.id === project.data?.baseline_version_id ? <Badge value="COMPLETED" label="baseline" /> : null}</>}>
              <div className="space-y-4">
                {list.map((c) => (
                  <div key={c.id} className="rounded-md border border-slate-200 p-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <div className="flex items-center gap-2"><Badge value={c.status} /><Mono>{c.endpoint}</Mono></div>
                        <div className="mt-1 text-xs text-slate-500">
                          {c.kind} · {c.environment} · auth header {c.auth_header ?? "none"} · credential {c.has_credential ? "stored (write-only)" : "none"} ·
                          timeout {c.timeout_ms} ms · concurrency {c.max_concurrency}{c.max_rps ? ` · ${c.max_rps} rps` : ""} · mapping <Mono>{short(c.mapping_hash)}</Mono>
                          {c.dependency_control_url && <> · dependency simulator <Mono>{c.dependency_control_url}</Mono></>}
                        </div>
                      </div>
                      <div className="flex gap-2">
                        <Button size="sm" onClick={() => setMappingOf(c)}>Field mapping</Button>
                        {has(...CAN.manageSystems) && <Button size="sm" variant="primary" onClick={() => verify(c)} disabled={busy === c.id}>{busy === c.id ? "Verifying…" : "Verify"}</Button>}
                      </div>
                    </div>
                    {c.verification && (
                      <ul className="mt-3 grid gap-1 text-xs md:grid-cols-2">
                        {c.verification.checks.map((ch) => (
                          <li key={ch.check} className="flex gap-2">
                            <span aria-label={ch.passed ? "passed" : "failed"} className={ch.passed ? "text-green-700" : ch.level === "warning" ? "text-amber-700" : "text-red-700"}>{ch.passed ? "✔" : ch.level === "warning" ? "!" : "✖"}</span>
                            <span><span className="font-medium">{ch.check.replace(/_/g, " ")}</span> — {ch.detail}</span>
                          </li>
                        ))}
                        <li className="text-slate-500 md:col-span-2">Verified {fmtDate(c.verified_at)} by {c.verification.verified_by}</li>
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            </Card>
          );
        })}
      </div>
      <Modal open={!!mappingOf} onOpenChange={(o) => !o && setMappingOf(null)} title={`Field mapping — version ${mappingOf ? vName(mappingOf.system_version_id) : ""}`} wide
             description="Customer fields are normalised into the common decision schema; every transformation is recorded in the evidence.">
        {mappingOf && <pre className="max-h-[60vh] overflow-auto rounded bg-slate-950 p-3 text-xs text-slate-100">{JSON.stringify(mappingOf.mapping, null, 2)}</pre>}
      </Modal>
    </>
  );
}
