"use client";

import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Field, Json, Loading, Modal, Mono, PageHeader, Select, Table, Tabs, Td, inputCls, useApi } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtDate, short } from "@/lib/format";
import type { Issue, PolicySet, PolicyTemplate, Project, TestSuite } from "@/lib/types";

type PolicyDoc = { label?: string; parameters?: Record<string, unknown>; policies?: Record<string, unknown>[]; change_controls?: Record<string, unknown>[]; expected_changes?: Record<string, unknown>[] };

function Issues({ issues }: { issues: Issue[] }) {
  if (!issues.length) return <p className="text-sm text-green-700">✔ No validation issues.</p>;
  return (
    <ul className="space-y-1 text-sm">
      {issues.map((i, n) => (
        <li key={n} className={i.level === "error" ? "text-red-800" : "text-amber-800"}>
          <span className="font-mono text-xs">{i.level.toUpperCase()} {i.code}</span> — {i.message}
        </li>
      ))}
    </ul>
  );
}

function RuleTable({ doc }: { doc: PolicyDoc }) {
  const params = doc.parameters ?? {};
  return (
    <div className="space-y-4">
      {Object.keys(params).length > 0 && (
        <div className="text-sm"><span className="font-medium">Institution-supplied parameters:</span>{" "}
          {Object.entries(params).map(([k, v]) => <span key={k} className="mr-3"><Mono>{k}</Mono> = <b>{String(v)}</b></span>)}</div>
      )}
      <Table head={["Policy", "Description", "Requires", "Severity", "Precedence"]}>
        {(doc.policies ?? []).map((p) => {
          const a = p.assert as { operator?: string; value?: unknown } | undefined;
          return (
            <tr key={String(p.policy_id)}>
              <Td><Mono>{String(p.policy_id)}</Mono>{p.enabled === false && <span className="ml-1 text-xs text-slate-500">(disabled)</span>}</Td>
              <Td className="max-w-md">{String(p.description ?? "")}</Td>
              <Td><Mono>decision {a?.operator} {JSON.stringify(a?.value)}</Mono></Td>
              <Td><Badge value={String(p.severity ?? "HIGH")} /></Td>
              <Td className="tabular-nums">{String(p.precedence ?? 0)}</Td>
            </tr>
          );
        })}
      </Table>
      {(doc.change_controls?.length ?? 0) > 0 && (
        <Table head={["Change control", "Description", "Change", "Action", "Severity"]}>
          {doc.change_controls!.map((c) => (
            <tr key={String(c.rule_id)}>
              <Td><Mono>{String(c.rule_id)}</Mono></Td><Td>{String(c.description ?? "")}</Td>
              <Td><Mono>{JSON.stringify(c.from)} → {JSON.stringify(c.to)}</Mono></Td><Td><Badge value={c.action === "PROHIBIT" ? "PROHIBITED" : "UNEXPECTED"} label={String(c.action)} /></Td>
              <Td><Badge value={String(c.severity)} /></Td>
            </tr>
          ))}
        </Table>
      )}
      {(doc.expected_changes?.length ?? 0) > 0 && (
        <Table head={["Approved change", "Description", "Change", "Reference"]}>
          {doc.expected_changes!.map((c) => (
            <tr key={String(c.change_id)}>
              <Td><Mono>{String(c.change_id)}</Mono></Td><Td>{String(c.description ?? "")}</Td>
              <Td><Mono>{JSON.stringify(c.from)} → {JSON.stringify(c.to)}</Mono></Td><Td>{String(c.reference ?? "")}</Td>
            </tr>
          ))}
        </Table>
      )}
    </div>
  );
}

function Preview({ content }: { content: string }) {
  const [input, setInput] = useState(JSON.stringify({ monthly_cash_available: 13000, monthly_debt_service: 10000, bureau: { status: "UNAVAILABLE" }, fraud: { status: "CLEAR" }, identity: { status: "VERIFIED" } }, null, 2));
  const [decision, setDecision] = useState("APPROVE");
  const [bureauFault, setBureauFault] = useState("NONE");
  const [result, setResult] = useState<{ derived: Record<string, unknown>; outcomes: { policy_id: string; status: string; expected: string; reason: string }[] } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const run = async () => {
    setError(null);
    try {
      setResult(await api.post("/v1/policy-sets/preview", { content: JSON.parse(content), input: JSON.parse(input), output: { decision },
        scenario: { faults: { bureau: bureauFault, fraud: "NONE", identity: "NONE" } } }));
    } catch (e) { setError(e instanceof ApiError ? e : new ApiError(0, "INVALID", "Input or policy is not valid JSON")); }
  };
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="space-y-3">
        <Field label="Sample decision input (canonical schema)"><textarea className={`${inputCls} font-mono text-xs`} rows={9} value={input} onChange={(e) => setInput(e.target.value)} spellCheck={false} /></Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Decision returned by the system"><Select value={decision} onChange={setDecision} options={["APPROVE", "REFER", "REVIEW", "HOLD", "DECLINE"].map((d) => ({ value: d, label: d }))} /></Field>
          <Field label="Injected bureau fault"><Select value={bureauFault} onChange={setBureauFault} options={["NONE", "TIMEOUT", "UNAVAILABLE", "MALFORMED", "MISSING_RESPONSE"].map((d) => ({ value: d, label: d }))} /></Field>
        </div>
        <Button onClick={run}>Evaluate</Button>
        <ErrorBox error={error} />
      </div>
      <div>
        {result ? (
          <>
            <div className="mb-2 text-xs text-slate-600">Derived: {Object.entries(result.derived).map(([k, v]) => <span key={k} className="mr-2"><Mono>{k}</Mono>={String(v)}</span>)}</div>
            <Table head={["Policy", "Outcome", "Requires", "Note"]}>
              {result.outcomes.map((o) => (
                <tr key={o.policy_id}>
                  <Td><Mono>{o.policy_id}</Mono></Td>
                  <Td><Badge value={o.status === "VIOLATED" ? "FAIL" : o.status === "SATISFIED" ? "PASS" : o.status === "UNKNOWN" ? "INCONCLUSIVE" : "SKIPPED"} label={o.status.replace("_", " ")} /></Td>
                  <Td className="text-xs">{o.expected}</Td><Td className="text-xs text-slate-600">{o.status === "NOT_APPLICABLE" ? "" : o.reason}</Td>
                </tr>
              ))}
            </Table>
          </>
        ) : <Empty>Evaluate a sample decision against this policy set.</Empty>}
      </div>
    </div>
  );
}

function PolicySets({ projectId }: { projectId: string }) {
  const { has, me } = useAuth();
  const list = useApi(() => api.get<PolicySet[]>(`/v1/policy-sets?project_id=${projectId}`), [projectId]);
  const [sel, setSel] = useState<string | null>(null);
  const current = useApi(() => (sel ? api.get<PolicySet>(`/v1/policy-sets/${sel}`) : Promise.resolve(null)), [sel]);
  const [text, setText] = useState("");
  const [validation, setValidation] = useState<{ valid: boolean; issues: Issue[] } | null>(null);
  const [diff, setDiff] = useState<Record<string, Record<string, unknown>> | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");

  useEffect(() => { if (!sel && list.data?.length) setSel(list.data[0].id); }, [list.data, sel]);
  useEffect(() => {
    const ps = current.data;
    if (!ps) return;
    setText(JSON.stringify(ps.content, null, 2));
    setValidation(ps.validation);
    setDiff(null);
    if (ps.parent_id) api.get<Record<string, Record<string, unknown>>>(`/v1/policy-sets/${ps.id}/diff?against=${ps.parent_id}`).then(setDiff).catch(() => setDiff(null));
  }, [current.data]);

  const doc = useMemo<PolicyDoc | null>(() => { try { return JSON.parse(text); } catch { return null; } }, [text]);
  const ps = current.data;
  const editable = !!ps && ["DRAFT", "REJECTED"].includes(ps.approval_status) && has(...CAN.editPolicy);
  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try { await fn(); await list.reload(); await current.reload(); } catch (e) { setError(e as ApiError); }
  };
  const validate = () => act(async () => setValidation(await api.post("/v1/policy-sets/validate", { content: JSON.parse(text) })));
  const authoredByMe = ps && me && (ps.created_by === me.id);

  const groups = (list.data ?? []).reduce<Record<string, PolicySet[]>>((acc, p) => { (acc[p.name] ??= []).push(p); return acc; }, {});
  return (
    <div className="grid gap-6 lg:grid-cols-4">
      <div className="space-y-3">
        {has(...CAN.editPolicy) && <Button variant="primary" onClick={() => setCreating(true)}>New policy set</Button>}
        {list.loading ? <Loading /> : !list.data?.length ? <Empty>No policy sets.</Empty> : Object.entries(groups).map(([name, versions]) => (
          <div key={name} className="rounded-md border border-slate-200 bg-white">
            <div className="border-b border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700">{name}</div>
            {versions.map((v) => (
              <button key={v.id} onClick={() => setSel(v.id)} aria-pressed={sel === v.id}
                      className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-slate-50 ${sel === v.id ? "bg-blue-50" : ""}`}>
                <span>v{v.version} {v.validation.valid ? "" : <span className="text-xs text-red-700">invalid</span>}</span><Badge value={v.approval_status} />
              </button>
            ))}
          </div>
        ))}
      </div>
      <div className="space-y-4 lg:col-span-3">
        <ErrorBox error={error} />
        {!ps ? <Empty>Select a policy set.</Empty> : (
          <>
            <Card title={<>{ps.name} v{ps.version} <Badge value={ps.approval_status} /></>}
                  actions={<>
                    {editable && <Button size="sm" onClick={validate}>Validate</Button>}
                    {editable && <Button size="sm" onClick={() => act(() => api.put(`/v1/policy-sets/${ps.id}`, { content: JSON.parse(text) }))}>Save draft</Button>}
                    {editable && <Button size="sm" variant="primary" onClick={() => act(() => api.post(`/v1/policy-sets/${ps.id}/submit`, { note }))}>Submit for approval</Button>}
                    {has(...CAN.editPolicy) && ps.approval_status === "APPROVED" && <Button size="sm" onClick={() => act(async () => { const n = await api.post<PolicySet>("/v1/policy-sets", { project_id: projectId, name: ps.name, content: ps.content, parent_id: ps.id }); setSel(n.id); })}>New version from this</Button>}
                  </>}>
              <div className="mb-3 grid gap-2 text-xs text-slate-600 md:grid-cols-3">
                <div>Label: <b>{(ps.content as PolicyDoc)?.label ?? "—"}</b></div>
                <div>Content hash: <Mono>{short(ps.content_hash, 16)}</Mono></div>
                <div>{ps.approved_at ? <>Approved {fmtDate(ps.approved_at)}</> : <>Created {fmtDate(ps.created_at)}</>}</div>
              </div>
              {ps.review_note && <p className="mb-3 rounded bg-slate-50 px-3 py-2 text-sm">Review note: {ps.review_note}</p>}
              {validation && <div className="mb-4"><Issues issues={validation.issues} /></div>}
              {doc ? <RuleTable doc={doc} /> : <p className="text-sm text-red-700">Policy definition is not valid JSON.</p>}
              {ps.approval_status === "PENDING_REVIEW" && has(...CAN.approvePolicy) && (
                <div className="mt-4 space-y-2 border-t border-slate-200 pt-4">
                  <p className="text-sm text-slate-700">Four-eyes review: the author or submitter of a policy set cannot approve it.{authoredByMe && <b> You authored this set.</b>}</p>
                  <input aria-label="Review note" className={inputCls} placeholder="Review note (required to reject)" value={note} onChange={(e) => setNote(e.target.value)} />
                  <div className="flex gap-2">
                    <Button variant="primary" onClick={() => act(() => api.post(`/v1/policy-sets/${ps.id}/approve`, { note }))}>Approve</Button>
                    <Button variant="danger" onClick={() => act(() => api.post(`/v1/policy-sets/${ps.id}/reject`, { note }))}>Reject</Button>
                  </div>
                </div>
              )}
            </Card>
            {diff && (
              <Card title="Changes from the previous version">
                <div className="grid gap-2 text-sm md:grid-cols-2">
                  {Object.entries(diff).map(([section, changes]) => (
                    <div key={section}><div className="text-xs font-semibold uppercase text-slate-500">{section.replace("_", " ")}</div>
                      {Object.keys(changes).length === 0 ? <div className="text-slate-400">No changes</div> :
                        Object.entries(changes).map(([k, v]) => <div key={k}><Mono>{k}</Mono>: {typeof v === "string" ? v : `${JSON.stringify((v as { from: unknown }).from)} → ${JSON.stringify((v as { to: unknown }).to)}`}</div>)}
                    </div>
                  ))}
                </div>
              </Card>
            )}
            <Tabs tabs={[
              { value: "preview", label: "Preview against a sample", content: <Card><Preview content={text} /></Card> },
              { value: "json", label: editable ? "Edit definition" : "Definition (read-only)", content: (
                <Card>
                  <textarea aria-label="Policy definition JSON" className={`${inputCls} font-mono text-xs`} rows={28} value={text} onChange={(e) => setText(e.target.value)} readOnly={!editable} spellCheck={false} />
                  <p className="mt-2 text-xs text-slate-500">Operators: eq, ne, gt, gte, lt, lte, in, not_in, contains, exists, not_exists, all, any, not. Policies are data — no code is ever executed.</p>
                </Card>) },
            ]} />
          </>
        )}
      </div>
      <NewPolicySet open={creating} onOpenChange={setCreating} projectId={projectId}
                    onCreated={async (id) => { setCreating(false); await list.reload(); setSel(id); }} />
    </div>
  );
}

function NewPolicySet({ open, onOpenChange, projectId, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; projectId: string; onCreated: (id: string) => void }) {
  const templates = useApi(() => api.get<PolicyTemplate[]>("/v1/policy-templates"), []);
  const [tid, setTid] = useState("");
  const [name, setName] = useState("");
  const [params, setParams] = useState<Record<string, string>>({});
  const [check, setCheck] = useState<{ valid: boolean; issues: Issue[] } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const t = templates.data?.find((x) => x.template_id === tid);
  useEffect(() => { if (!tid && templates.data?.length) setTid(templates.data[0].template_id); }, [templates.data, tid]);
  useEffect(() => { setParams({}); setCheck(null); if (t && !name) setName(t.name); }, [tid]); // eslint-disable-line react-hooks/exhaustive-deps

  const values = () => Object.fromEntries((t?.parameters ?? []).filter((p) => params[p.name]?.trim()).map((p) => [p.name, Number(params[p.name])]));
  const badNumber = (t?.parameters ?? []).some((p) => params[p.name]?.trim() && Number.isNaN(Number(params[p.name])));
  const create = async () => {
    setError(null);
    try {
      const r = await api.post<{ content: unknown; validation: { valid: boolean; issues: Issue[] } }>(`/v1/policy-templates/${tid}/instantiate`, { parameters: values() });
      setCheck(r.validation);
      const n = await api.post<PolicySet>("/v1/policy-sets", { project_id: projectId, name, content: r.content });
      setName(""); setParams({}); setCheck(null); onCreated(n.id);
    } catch (e) { setError(e as ApiError); }
  };
  return (
    <Modal open={open} onOpenChange={onOpenChange} wide title="New policy set"
           description="Start from a template, enter your institution's own thresholds, then review and submit for approval. Every threshold starts empty: ModelAssure never chooses them for you.">
      {templates.loading ? <Loading /> : (
        <div className="space-y-4">
          <fieldset><legend className="mb-2 text-sm font-medium text-slate-700">Template</legend>
            <div className="grid gap-2 md:grid-cols-2">
              {templates.data?.map((x) => (
                <label key={x.template_id} className={`cursor-pointer rounded-md border p-3 text-sm ${tid === x.template_id ? "border-blue-600 bg-blue-50" : "border-slate-200"}`}>
                  <input type="radio" name="template" className="sr-only" checked={tid === x.template_id} onChange={() => setTid(x.template_id)} />
                  <div className="font-medium">{x.name}</div>
                  <div className="mt-1 text-xs text-slate-600">{x.summary}</div>
                  <div className="mt-1 text-xs text-slate-500">{x.counts.policies} policies · {x.counts.change_controls} change controls</div>
                </label>
              ))}
            </div>
          </fieldset>
          {t && (
            <>
              {t.legal_note && <p className="rounded bg-amber-50 px-3 py-2 text-xs text-amber-900">{t.legal_note}</p>}
              <Field label="Policy set name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} /></Field>
              <div className="grid gap-3 md:grid-cols-2">
                {t.parameters.map((p) => (
                  <Field key={p.name} label={p.label} hint={p.help}
                         error={params[p.name]?.trim() && Number.isNaN(Number(params[p.name])) ? "Enter a number" : undefined}>
                    <input className={inputCls} inputMode="decimal" placeholder="Your approved value" value={params[p.name] ?? ""}
                           onChange={(e) => setParams({ ...params, [p.name]: e.target.value })} />
                  </Field>
                ))}
              </div>
              <details className="text-sm"><summary className="cursor-pointer text-blue-800">Policies in this template</summary>
                <ul className="mt-2 space-y-1">{t.policies.map((p) => <li key={p.policy_id}><Mono>{p.policy_id}</Mono> <Badge value={p.severity} /> {p.description}</li>)}</ul>
                <p className="mt-2 text-xs text-slate-500">Input fields the policies read: {t.required_input_fields.join(", ")}.</p>
              </details>
              {t.parameters.some((p) => !params[p.name]?.trim()) && (
                <p className="text-xs text-slate-600">You can create the draft now and fill in thresholds later; it can&apos;t be submitted for approval until every threshold has a value.</p>
              )}
              {check && <Issues issues={check.issues} />}
              <ErrorBox error={error} />
              <Button variant="primary" disabled={!name || badNumber} onClick={create}>Create draft</Button>
            </>
          )}
        </div>
      )}
    </Modal>
  );
}

const PROFILES = [
  { value: "sme_lending", label: "SME lending (DSCR-based affordability)", suite: "SME lending — standard release suite" },
  { value: "consumer_credit", label: "Consumer credit / BNPL (debt-service ratio)", suite: "Consumer credit — standard release suite" },
];

const GENERATORS = [
  ["normal", "Normal cases"], ["boundary", "Boundary testing (thresholds from policies)"], ["missing_data", "Missing data"],
  ["dependency_failure", "Dependency failures (bureau, fraud, identity)"], ["cross_system_conflict", "Credit/fraud conflicts"],
  ["duplicate", "Duplicate requests"], ["invalid_input", "Invalid input"], ["property", "Version regression (property-based)"],
] as const;

function Suites({ projectId }: { projectId: string }) {
  const { has } = useAuth();
  const suites = useApi(() => api.get<TestSuite[]>(`/v1/test-suites?project_id=${projectId}`), [projectId]);
  const policySets = useApi(() => api.get<PolicySet[]>(`/v1/policy-sets?project_id=${projectId}`), [projectId]);
  const project = useApi(() => api.get<Project>(`/v1/projects/${projectId}`), [projectId]);
  const [profile, setProfile] = useState("sme_lending");
  useEffect(() => {
    if (project.data?.system?.category === "CONSUMER_CREDIT_DECISION") { setProfile("consumer_credit"); setName(PROFILES[1].suite); }
  }, [project.data]);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("SME lending — standard release suite");
  const [seed, setSeed] = useState("20261124");
  const [count, setCount] = useState("1000");
  const [gens, setGens] = useState<Set<string>>(new Set(GENERATORS.map((g) => g[0])));
  const [psIds, setPsIds] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<{ case_count: number; categories: Record<string, number> } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [casesOf, setCasesOf] = useState<TestSuite | null>(null);
  const cases = useApi(() => (casesOf ? api.get<{ total: number; items: { case_key: string; category: string; input: unknown; scenario: Record<string, unknown>; tags: string[] }[] }>(`/v1/test-suites/${casesOf.id}/cases?limit=60`) : Promise.resolve(null)), [casesOf?.id]);

  const body = () => ({ project_id: projectId, name, config: { seed: Number(seed), profile, generators: [...gens].map((g) => (g === "property" ? { type: g, count: Number(count) } : g === "duplicate" ? { type: g, repeat: 3 } : { type: g })) }, policy_set_ids: [...psIds] });
  const toggle = (s: Set<string>, v: string, set: (x: Set<string>) => void) => { const n = new Set(s); if (n.has(v)) n.delete(v); else n.add(v); set(n); };

  return (
    <div className="space-y-4">
      {has(...CAN.createSuite) && <Button variant="primary" onClick={() => setOpen(true)}>New test suite</Button>}
      <Card pad={false}>
        <Table head={["Suite", "Cases", "Categories", "Seed", "Content hash", "Created", ""]}
               empty={!suites.data?.length && <div className="p-4"><Empty>No test suites yet.</Empty></div>}>
          {suites.data?.map((s) => (
            <tr key={s.id}>
              <Td><span className="font-medium">{s.name}</span> <span className="text-xs text-slate-500">v{s.version}</span></Td>
              <Td className="tabular-nums">{s.case_count.toLocaleString()}</Td>
              <Td className="max-w-md text-xs">{Object.entries(s.category_counts).map(([k, v]) => `${k.toLowerCase().replace(/_/g, " ")} ${v}`).join(" · ")}</Td>
              <Td><Mono>{s.seed}</Mono></Td><Td><Mono>{short(s.content_hash)}</Mono></Td>
              <Td className="text-xs text-slate-500">{fmtDate(s.created_at)}</Td>
              <Td><Button size="sm" onClick={() => setCasesOf(s)}>Cases</Button></Td>
            </tr>
          ))}
        </Table>
      </Card>
      <p className="text-xs text-slate-500">Cases are generated once when a suite version is created, then stored and hashed. Runs replay stored cases, so results are reproducible. Generated cases are synthetic.</p>
      <Modal open={open} onOpenChange={setOpen} wide title="New test suite" description="Version 1 scenario generation is deterministic. Boundary values come from the thresholds in the selected policy sets.">
        <div className="grid gap-4 md:grid-cols-3">
          <div className="md:col-span-2"><Field label="Name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} /></Field></div>
          <Field label="Seed"><input className={inputCls} value={seed} onChange={(e) => setSeed(e.target.value)} /></Field>
        </div>
        <div className="mt-4"><Field label="Product profile" hint="Shapes the synthetic applicants: which financial fields they carry and what a healthy or stretched case looks like.">
          <Select value={profile} onChange={(v) => { setProfile(v); setName(PROFILES.find((p) => p.value === v)!.suite); setPreview(null); }} options={PROFILES} />
        </Field></div>
        <fieldset className="mt-4"><legend className="mb-2 text-sm font-medium text-slate-700">Scenario categories</legend>
          <div className="grid gap-2 md:grid-cols-2">
            {GENERATORS.map(([g, label]) => (
              <label key={g} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={gens.has(g)} onChange={() => toggle(gens, g, setGens)} />{label}
                {g === "property" && gens.has(g) && <input aria-label="Property case count" className={`${inputCls} ml-2 w-24`} value={count} onChange={(e) => setCount(e.target.value)} />}</label>
            ))}
          </div>
        </fieldset>
        <fieldset className="mt-4"><legend className="mb-2 text-sm font-medium text-slate-700">Policy sets for boundary thresholds</legend>
          <div className="grid gap-1 md:grid-cols-2">
            {(policySets.data ?? []).map((p) => <label key={p.id} className="flex items-center gap-2 text-sm"><input type="checkbox" checked={psIds.has(p.id)} onChange={() => toggle(psIds, p.id, setPsIds)} />{p.name} v{p.version} <Badge value={p.approval_status} /></label>)}
          </div>
        </fieldset>
        {preview && <p className="mt-4 text-sm">Preview: <b>{preview.case_count}</b> cases — {Object.entries(preview.categories).map(([k, v]) => `${k} ${v}`).join(", ")}</p>}
        <div className="mt-4 space-y-3"><ErrorBox error={error} />
          <div className="flex gap-2">
            <Button onClick={async () => { setError(null); try { setPreview(await api.post("/v1/test-suites/preview", body())); } catch (e) { setError(e as ApiError); } }}>Preview</Button>
            <Button variant="primary" onClick={async () => { setError(null); try { await api.post("/v1/test-suites", body()); setOpen(false); await suites.reload(); } catch (e) { setError(e as ApiError); } }}>Create suite</Button>
          </div></div>
      </Modal>
      <Modal open={!!casesOf} onOpenChange={(o) => !o && setCasesOf(null)} wide title={`${casesOf?.name ?? ""} — sample cases`} description={`${cases.data?.total ?? "…"} cases in this suite version (first 60 shown).`}>
        {cases.loading ? <Loading /> : (
          <Table head={["Case", "Category", "Faults", "Tags", "Input"]}>
            {cases.data?.items.map((c) => (
              <tr key={c.case_key}><Td><Mono>{c.case_key}</Mono></Td><Td className="text-xs">{c.category}</Td>
                <Td className="text-xs">{Object.entries((c.scenario.faults as Record<string, string>) ?? {}).filter(([, v]) => v !== "NONE").map(([k, v]) => `${k}=${v}`).join(", ") || "—"}</Td>
                <Td className="text-xs">{c.tags.join(", ")}</Td><Td><details><summary className="cursor-pointer text-xs text-blue-800">view</summary><Json value={c.input} /></details></Td></tr>
            ))}
          </Table>
        )}
      </Modal>
    </div>
  );
}

export default function BuilderPage() {
  const { id } = useParams<{ id: string }>();
  return (
    <>
      <PageHeader title="Policy & test builder" subtitle="Steps 3–4: the institution's approved rules and the scenarios used to test them. ModelAssure never invents credit or regulatory thresholds." />
      <Tabs tabs={[
        { value: "policies", label: "Policy sets", content: <PolicySets projectId={id} /> },
        { value: "suites", label: "Test suites", content: <Suites projectId={id} /> },
      ]} />
    </>
  );
}
