"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, Card, ErrorBox, Field, PageHeader, Select, inputCls, useApi } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import type { Project, System, SystemVersion, UserRef } from "@/lib/types";

export default function NewProject() {
  const router = useRouter();
  const { has, me } = useAuth();
  const systems = useApi(() => api.get<System[]>("/v1/systems"), []);
  const users = useApi(() => api.get<UserRef[]>("/v1/users"), []);
  const [systemId, setSystemId] = useState("");
  const versions = useApi(() => (systemId ? api.get<SystemVersion[]>(`/v1/systems/${systemId}/versions`) : Promise.resolve([])), [systemId]);
  const [form, setForm] = useState({ name: "", business_use_case: "", owner_id: "", baseline_version_id: "", candidate_version_id: "", risk_classification: "HIGH", test_environment: "" });
  const [newSystem, setNewSystem] = useState({ name: "", vendor: "", category: "SME_LENDING_DECISION" });
  const [newVersion, setNewVersion] = useState({ version: "", expected_model_version: "", description: "" });
  const [error, setError] = useState<ApiError | null>(null);
  const set = (k: keyof typeof form) => (v: string) => setForm((f) => ({ ...f, [k]: v }));

  const createSystem = async () => {
    setError(null);
    try {
      const s = await api.post<System>("/v1/systems", { name: newSystem.name, vendor: newSystem.vendor || null, category: newSystem.category });
      await systems.reload();
      setSystemId(s.id);
      setNewSystem({ name: "", vendor: "", category: "SME_LENDING_DECISION" });
    } catch (e) { setError(e as ApiError); }
  };
  const registerVersion = async () => {
    setError(null);
    try {
      await api.post(`/v1/systems/${systemId}/versions`, { version: newVersion.version, expected_model_version: newVersion.expected_model_version || null, description: newVersion.description || null });
      setNewVersion({ version: "", expected_model_version: "", description: "" });
      await versions.reload();
    } catch (e) { setError(e as ApiError); }
  };
  const create = async () => {
    setError(null);
    try {
      const p = await api.post<Project>("/v1/projects", { ...form, system_id: systemId, owner_id: form.owner_id || me?.id });
      router.push(`/projects/${p.id}`);
    } catch (e) { setError(e as ApiError); }
  };

  const vOpts = (versions.data ?? []).map((v) => ({ value: v.id, label: `${v.version}${v.description ? ` — ${v.description}` : ""}` }));
  return (
    <>
      <PageHeader title="New validation project" subtitle="Step 1 of the workflow: describe the release under test. Versions are immutable once registered." />
      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <Card title="Release under test">
            <div className="grid gap-4 md:grid-cols-2">
              <Field label="Project name"><input className={inputCls} value={form.name} onChange={(e) => set("name")(e.target.value)} placeholder="SME Lending v3.2 release" /></Field>
              <Field label="Risk classification"><Select value={form.risk_classification} onChange={set("risk_classification")} options={["HIGH", "MEDIUM", "LOW"].map((v) => ({ value: v, label: v }))} /></Field>
              <div className="md:col-span-2"><Field label="Business use case"><textarea className={inputCls} rows={2} value={form.business_use_case} onChange={(e) => set("business_use_case")(e.target.value)} placeholder="Automated SME working-capital lending decisions" /></Field></div>
              <Field label="Decision system">
                <Select value={systemId} onChange={(v) => { setSystemId(v); set("baseline_version_id")(""); set("candidate_version_id")(""); }}
                        options={(systems.data ?? []).map((s) => ({ value: s.id, label: s.name }))} placeholder="Select a registered system" />
              </Field>
              <Field label="System owner"><Select value={form.owner_id} onChange={set("owner_id")} options={(users.data ?? []).map((u) => ({ value: u.id, label: `${u.name} (${u.roles.join(", ")})` }))} placeholder="Me" /></Field>
              <Field label="Baseline version (current)"><Select value={form.baseline_version_id} onChange={set("baseline_version_id")} options={vOpts} placeholder="Select…" disabled={!systemId} /></Field>
              <Field label="Candidate version (proposed)"><Select value={form.candidate_version_id} onChange={set("candidate_version_id")} options={vOpts} placeholder="Select…" disabled={!systemId} /></Field>
              <div className="md:col-span-2"><Field label="Test environment" hint="Authorised sandbox only. ModelAssure never tests production endpoints."><input className={inputCls} value={form.test_environment} onChange={(e) => set("test_environment")(e.target.value)} placeholder="Vendor UAT sandbox" /></Field></div>
            </div>
            <div className="mt-4 flex items-center gap-3">
              <Button variant="primary" onClick={create} disabled={!has(...CAN.createProject) || !systemId || !form.baseline_version_id || !form.candidate_version_id}>Create project</Button>
              {!has(...CAN.createProject) && <span className="text-xs text-slate-500">Only an Admin or Risk Manager can create projects.</span>}
            </div>
            <div className="mt-3"><ErrorBox error={error} /></div>
          </Card>
        </div>
        <div className="space-y-6">
          <Card title="Register a decision system">
            {has(...CAN.manageSystems) ? (
              <div className="space-y-3">
                <Field label="Name"><input className={inputCls} value={newSystem.name} onChange={(e) => setNewSystem({ ...newSystem, name: e.target.value })} /></Field>
                <Field label="Vendor"><input className={inputCls} value={newSystem.vendor} onChange={(e) => setNewSystem({ ...newSystem, vendor: e.target.value })} /></Field>
                <Field label="Decision type"><Select value={newSystem.category} onChange={(v) => setNewSystem({ ...newSystem, category: v })}
                  options={[{ value: "SME_LENDING_DECISION", label: "SME lending" }, { value: "CONSUMER_CREDIT_DECISION", label: "Consumer credit / BNPL" }]} /></Field>
                <Button onClick={createSystem} disabled={!newSystem.name}>Register system</Button>
              </div>
            ) : <p className="text-sm text-slate-600">Ask an Engineer to register the decision system and its versions.</p>}
          </Card>
          {systemId && has(...CAN.manageSystems) && (
            <Card title="Register a version">
              <div className="space-y-3">
                <Field label="Version"><input className={inputCls} value={newVersion.version} onChange={(e) => setNewVersion({ ...newVersion, version: e.target.value })} placeholder="3.2.0" /></Field>
                <Field label="Expected model version" hint="The endpoint must report this, or results are flagged as VERSION_MISMATCH."><input className={inputCls} value={newVersion.expected_model_version} onChange={(e) => setNewVersion({ ...newVersion, expected_model_version: e.target.value })} /></Field>
                <Field label="Description"><input className={inputCls} value={newVersion.description} onChange={(e) => setNewVersion({ ...newVersion, description: e.target.value })} /></Field>
                <Button onClick={registerVersion} disabled={!newVersion.version}>Register version</Button>
              </div>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
