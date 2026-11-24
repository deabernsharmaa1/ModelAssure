"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { Badge, Button, Card, ErrorBox, Field, Loading, Mono, PageHeader, Select, Stat, Table, Td, inputCls, useApi } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtDate, short } from "@/lib/format";
import type { Project, Run, SetupStatus, SystemVersion, UserRef } from "@/lib/types";

const STEP_LINK: Record<string, string> = { versions: "", connections: "/connections", policies: "/builder", suite: "/builder", run: "/runs", review: "/release" };

function SetupChecklist({ projectId, refreshKey }: { projectId: string; refreshKey: unknown }) {
  const setup = useApi(() => api.get<SetupStatus>(`/v1/projects/${projectId}/setup`), [projectId, refreshKey]);
  const s = setup.data;
  if (!s) return null;
  return (
    <Card title={<>Setup checklist <span className="ml-2 text-xs font-normal text-slate-500">{s.completed} of {s.total} done</span></>}>
      <ol className="grid gap-2 md:grid-cols-3">
        {s.steps.map((st, i) => {
          const next = st.key === s.next_step;
          const href = `/projects/${projectId}${STEP_LINK[st.key]}`;
          return (
            <li key={st.key} className={`rounded-md border p-3 text-sm ${next ? "border-blue-600 bg-blue-50" : "border-slate-200"}`}>
              <div className="flex items-start gap-2">
                <span aria-label={st.done ? "done" : "not done"} className={`mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs ${st.done ? "bg-green-600 text-white" : "border border-slate-300 text-slate-500"}`}>{st.done ? "✓" : i + 1}</span>
                <div>
                  <div className="font-medium">{STEP_LINK[st.key] && !st.done ? <Link href={href} className="hover:underline">{st.title}</Link> : st.title}</div>
                  <div className="text-xs text-slate-500">{st.role}</div>
                  <div className="mt-1 text-xs text-slate-600">{st.detail}</div>
                  {next && <div className="mt-1 text-xs font-medium text-blue-800">Next step</div>}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

export default function ProjectSetup() {
  const { id } = useParams<{ id: string }>();
  const { has } = useAuth();
  const project = useApi(() => api.get<Project>(`/v1/projects/${id}`), [id]);
  const versions = useApi(() => (project.data ? api.get<SystemVersion[]>(`/v1/systems/${project.data.system_id}/versions`) : Promise.resolve([])), [project.data?.system_id]);
  const runs = useApi(() => api.get<Run[]>(`/v1/runs?project_id=${id}`), [id]);
  const users = useApi(() => api.get<UserRef[]>("/v1/users"), []);
  const [nv, setNv] = useState({ version: "", expected_model_version: "", description: "" });
  const [error, setError] = useState<ApiError | null>(null);

  if (project.loading) return <Loading />;
  if (!project.data) return <ErrorBox error={project.error} />;
  const p = project.data;
  const owner = users.data?.find((u) => u.id === p.owner_id);
  const vOpts = (versions.data ?? []).map((v) => ({ value: v.id, label: v.version }));

  const patch = async (body: Record<string, string>) => {
    setError(null);
    try { await api.patch(`/v1/projects/${id}`, body); await project.reload(); } catch (e) { setError(e as ApiError); }
  };
  const register = async () => {
    setError(null);
    try {
      await api.post(`/v1/systems/${p.system_id}/versions`, { version: nv.version, expected_model_version: nv.expected_model_version || null, description: nv.description || null });
      setNv({ version: "", expected_model_version: "", description: "" });
      await versions.reload();
    } catch (e) { setError(e as ApiError); }
  };

  const full = (runs.data ?? []).filter((r) => r.scope === "FULL");
  return (
    <>
      <PageHeader title={p.name} crumbs={<Link href="/" className="hover:underline">Dashboard</Link>}
                  subtitle={<>{p.business_use_case} · <Badge value={p.risk_classification} label={`${p.risk_classification} risk`} /></>}
                  actions={<Link href={`/projects/${id}/runs`} className="rounded-md bg-[#0b2545] px-3 py-1.5 text-sm font-medium text-white">Test execution</Link>} />
      <ErrorBox error={error} />
      <div className="mb-6"><SetupChecklist projectId={id} refreshKey={`${p.baseline_version_id}-${p.candidate_version_id}`} /></div>
      <div className="mb-6 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Baseline" value={p.baseline_version?.version ?? "—"} />
        <Stat label="Candidate" value={p.candidate_version?.version ?? "—"} tone="blue" />
        <Stat label="Full runs" value={full.length} />
        <Stat label="Latest assessment" value={full[0]?.assessment?.outcome ?? "—"} tone={full[0]?.assessment?.outcome === "PASS" ? "green" : full[0]?.assessment?.outcome === "FAIL" ? "red" : "amber"} />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Project details">
          <dl className="grid grid-cols-3 gap-y-2 text-sm">
            <dt className="text-slate-500">System</dt><dd className="col-span-2">{p.system?.name} <span className="text-xs text-slate-500">({p.system?.vendor ?? "no vendor"} · {p.system?.category === "CONSUMER_CREDIT_DECISION" ? "consumer credit / BNPL" : "SME lending"})</span></dd>
            <dt className="text-slate-500">Owner</dt><dd className="col-span-2">{owner ? `${owner.name} · ${owner.email}` : "—"}</dd>
            <dt className="text-slate-500">Test environment</dt><dd className="col-span-2">{p.test_environment}</dd>
            <dt className="text-slate-500">Created</dt><dd className="col-span-2">{fmtDate(p.created_at)}</dd>
          </dl>
          {has(...CAN.createProject) && (
            <div className="mt-4 grid gap-3 border-t border-slate-200 pt-4 md:grid-cols-2">
              <Field label="Baseline version"><Select value={p.baseline_version_id} onChange={(v) => patch({ baseline_version_id: v })} options={vOpts} /></Field>
              <Field label="Candidate version"><Select value={p.candidate_version_id} onChange={(v) => patch({ candidate_version_id: v })} options={vOpts} /></Field>
            </div>
          )}
        </Card>
        <Card title="Version registry" pad={false}>
          <Table head={["Version", "Expected model version", "Checksum", "Registered"]}>
            {versions.data?.map((v) => (
              <tr key={v.id}>
                <Td><span className="font-medium">{v.version}</span>{v.id === p.candidate_version_id && <span className="ml-2"><Badge value="RUNNING" label="candidate" /></span>}
                  {v.id === p.baseline_version_id && <span className="ml-2"><Badge value="COMPLETED" label="baseline" /></span>}
                  <div className="text-xs text-slate-500">{v.description}</div></Td>
                <Td><Mono>{v.expected_model_version ?? "—"}</Mono></Td>
                <Td><Mono>{short(v.checksum)}</Mono></Td>
                <Td className="text-xs text-slate-500">{fmtDate(v.created_at)}</Td>
              </tr>
            ))}
          </Table>
          {has(...CAN.manageSystems) && (
            <div className="grid gap-2 border-t border-slate-200 p-4 md:grid-cols-4">
              <input aria-label="Version" className={inputCls} placeholder="Version" value={nv.version} onChange={(e) => setNv({ ...nv, version: e.target.value })} />
              <input aria-label="Expected model version" className={inputCls} placeholder="Expected model version" value={nv.expected_model_version} onChange={(e) => setNv({ ...nv, expected_model_version: e.target.value })} />
              <input aria-label="Description" className={inputCls} placeholder="Description" value={nv.description} onChange={(e) => setNv({ ...nv, description: e.target.value })} />
              <Button onClick={register} disabled={!nv.version}>Register version</Button>
            </div>
          )}
        </Card>
      </div>
      <p className="mt-4 text-xs text-slate-500">Registered versions are immutable. A change to a version creates a new version record; historical test evidence is never altered.</p>
    </>
  );
}
