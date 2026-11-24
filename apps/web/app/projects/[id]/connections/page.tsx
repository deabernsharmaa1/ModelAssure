"use client";

import { useParams } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Field, Loading, Modal, Mono, PageHeader, Select, inputCls, useApi } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtDate, short } from "@/lib/format";
import type { Connection, MappingSuggestion, Project, SystemVersion } from "@/lib/types";

const EXAMPLE_MAPPING = {
  request: { fields: { applicationId: "case_id", productCode: "product", requestedAmount: "requested_amount", currency: "currency",
    monthlyCashAvailable: "monthly_cash_available", monthlyDebtService: "monthly_debt_service", "bureau.status": "bureau.status",
    "bureau.score": "bureau.score", "fraud.status": "fraud.status", "fraud.alertResolved": "fraud.alert_resolved", "identity.status": "identity.status" } },
  response: { fields: { case_id: { source: "applicationId" },
    decision: { source: "finalStatus", values: { APPROVED: "APPROVE", DECLINED: "DECLINE", MANUAL_REVIEW: "REVIEW", ON_HOLD: "HOLD", REFERRED: "REFER" } },
    pd: { source: "probabilityOfDefault" }, model_version: { source: "modelVersion" }, policy_version: { source: "policyVersion" },
    reason_codes: { source: "reasonCodes" }, trace_id: { source: "traceId" } } },
};

function SuggestMapping({ onUse }: { onUse: (mapping: Record<string, unknown>) => void }) {
  const [req, setReq] = useState("");
  const [resp, setResp] = useState("");
  const [result, setResult] = useState<MappingSuggestion | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const run = async () => {
    setError(null); setResult(null);
    let sampleRequest: unknown = null, responses: unknown;
    try {
      if (req.trim()) sampleRequest = JSON.parse(req);
      responses = JSON.parse(resp);
    } catch { setError(new ApiError(0, "INVALID", "Samples must be valid JSON")); return; }
    try {
      setResult(await api.post<MappingSuggestion>("/v1/connections/suggest-mapping", {
        sample_request: sampleRequest, sample_responses: Array.isArray(responses) ? responses : [responses] }));
    } catch (e) { setError(e as ApiError); }
  };
  return (
    <div className="space-y-3 rounded-md border border-blue-200 bg-blue-50/40 p-3">
      <p className="text-sm text-slate-700">Paste one request your API accepts and one or more responses it returns (an array is fine; include different decisions if you can). ModelAssure drafts the mapping. Nothing is sent to your system.</p>
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Sample request (optional)"><textarea aria-label="Sample request" className={`${inputCls} font-mono text-xs`} rows={7} value={req} onChange={(e) => setReq(e.target.value)} spellCheck={false} placeholder='{"applicationId": "A-1", "requestedAmount": 100000, ...}' /></Field>
        <Field label="Sample response(s)"><textarea aria-label="Sample responses" className={`${inputCls} font-mono text-xs`} rows={7} value={resp} onChange={(e) => setResp(e.target.value)} spellCheck={false} placeholder='[{"applicationId": "A-1", "finalStatus": "APPROVED"}, ...]' /></Field>
      </div>
      <Button size="sm" onClick={run} disabled={!resp.trim()}>Suggest mapping</Button>
      <ErrorBox error={error} />
      {result && (
        <div className="space-y-2 text-sm">
          <div className="grid gap-1 md:grid-cols-2">
            {result.fields.map((f) => (
              <div key={`${f.direction}-${f.canonical_field}`} className="text-xs">
                <span className="text-slate-500">{f.direction}</span> <Mono>{f.customer_field}</Mono> → <Mono>{f.canonical_field}</Mono>
                {f.matched_by === "synonym" && <span className="ml-1 text-amber-700">(by synonym, check it)</span>}
              </div>
            ))}
          </div>
          {Object.keys(result.decision_values).length > 0 && (
            <p className="text-xs">Decision values: {Object.entries(result.decision_values).map(([k, v]) => <span key={k} className="mr-2"><Mono>{k}</Mono> → <b>{v}</b></span>)}</p>
          )}
          {result.type_mismatches.map((m) => (
            <p key={m.customer_field} className="text-xs text-amber-800"><Mono>{m.customer_field}</Mono> looks like <Mono>{m.canonical_field}</Mono>, but its sample is a {m.sample_type} and ModelAssure expects a {m.expected}. {m.note}</p>
          ))}
          {(result.unmatched.request.length + result.unmatched.response.length) > 0 && (
            <p className="text-xs text-slate-600">Not recognised (map by hand if they matter): {[...result.unmatched.request, ...result.unmatched.response].join(", ")}</p>
          )}
          {result.canonical_inputs_not_found.length > 0 && (
            <p className="text-xs text-slate-600">ModelAssure fields not in your sample request: {result.canonical_inputs_not_found.join(", ")}. Only the ones your policies read matter (each template lists them); a policy that reads a missing field is UNKNOWN, never a pass.</p>
          )}
          {result.notes.map((n, i) => <p key={i} className="text-xs text-slate-700">• {n}</p>)}
          {result.response_checks.some((c) => !c.ok) && <p className="text-xs text-red-800">Some samples don&apos;t normalise yet: {result.response_checks.filter((c) => !c.ok).flatMap((c) => c.errors).join("; ")}</p>}
          <Button size="sm" variant="primary" disabled={!result.valid} onClick={() => onUse(result.mapping)}>Use this mapping</Button>
        </div>
      )}
    </div>
  );
}

function NewConnection({ versions, onCreated }: { versions: SystemVersion[]; onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ system_version_id: "", endpoint: "", auth_header: "X-API-Key", credential: "", timeout_ms: "5000", max_concurrency: "5", max_rps: "", dependency_control_url: "", dependency_credential: "" });
  const [mapping, setMapping] = useState("{}");
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
            <SuggestMapping onUse={(m) => setMapping(JSON.stringify(m, null, 2))} />
          </div>
          <div className="md:col-span-2">
            <Field label="Field mapping (customer ↔ common decision schema)" hint="Empty {} means your API already uses ModelAssure's field names. request.fields: customer field ← canonical field. response.fields: canonical field ← customer field, with decision value mapping.">
              <textarea aria-label="Field mapping JSON" className={`${inputCls} font-mono text-xs`} rows={12} value={mapping} onChange={(e) => setMapping(e.target.value)} spellCheck={false} />
            </Field>
            <button type="button" className="mt-1 text-xs text-blue-800 underline" onClick={() => setMapping(JSON.stringify(EXAMPLE_MAPPING, null, 2))}>Load the fictional bank&apos;s example mapping</button>
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
