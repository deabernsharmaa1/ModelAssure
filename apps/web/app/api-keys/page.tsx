"use client";

import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorBox, Field, Loading, Mono, PageHeader, Select, Table, Td, inputCls, useApi } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { CAN, useAuth } from "@/lib/auth";
import { fmtDate } from "@/lib/format";
import type { Project } from "@/lib/types";

interface KeyRow { id: string; key_id: string; prefix: string; name: string; project_id: string; created_at: string; expires_at: string; last_used_at: string | null; revoked_at: string | null; status: string }

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function ApiKeysPage() {
  const { has } = useAuth();
  const keys = useApi(() => api.get<KeyRow[]>("/v1/api-keys"), []);
  const projects = useApi(() => api.get<Project[]>("/v1/projects"), []);
  const [f, setF] = useState({ name: "", project_id: "", expires_in_days: "90" });
  const [created, setCreated] = useState<(KeyRow & { key: string }) | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  if (!has(...CAN.manageKeys)) return <Empty>Only administrators manage pipeline keys.</Empty>;
  const pname = (id: string) => projects.data?.find((p) => p.id === id)?.name ?? id;

  const create = async () => {
    setError(null);
    try {
      const k = await api.post<KeyRow & { key: string }>("/v1/api-keys", { ...f, expires_in_days: Number(f.expires_in_days) });
      setCreated(k); setF({ name: "", project_id: "", expires_in_days: "90" }); await keys.reload();
    } catch (e) { setError(e as ApiError); }
  };
  const revoke = async (k: KeyRow) => {
    setError(null);
    try { await api.post(`/v1/api-keys/${k.id}/revoke`); await keys.reload(); } catch (e) { setError(e as ApiError); }
  };

  return (
    <>
      <PageHeader title="Pipeline keys" subtitle="Keys for CI/CD pipelines to run the release gate. A key can only start and read gate runs for one project; it cannot read findings, change policies or approve anything." />
      <div className="space-y-6">
        <Card title="New key">
          <div className="grid items-end gap-3 md:grid-cols-4">
            <Field label="Name" hint="e.g. GitHub Actions — decision-engine repo"><input className={inputCls} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
            <Field label="Project" hint="The key can act on this project only"><Select value={f.project_id} onChange={(v) => setF({ ...f, project_id: v })} placeholder="Select…" options={(projects.data ?? []).map((p) => ({ value: p.id, label: p.name }))} /></Field>
            <Field label="Expires after (days)" hint="1–365"><input className={inputCls} type="number" min={1} max={365} value={f.expires_in_days} onChange={(e) => setF({ ...f, expires_in_days: e.target.value })} /></Field>
            <div className="pb-5"><Button variant="primary" disabled={!f.name || !f.project_id} onClick={create}>Create key</Button></div>
          </div>
          <ErrorBox error={error} />
          <p className="mt-2 text-xs text-slate-500">You will be treated as having started every run this key starts, so you cannot review those releases yourself.</p>
        </Card>
          {created && (
            <Card title="Copy this key now">
              <p className="mb-2 text-sm text-amber-900">It is shown only once. Store it in your pipeline&apos;s secret store as <Mono>MODELASSURE_API_KEY</Mono>.</p>
              <div className="flex items-center gap-2">
                <code className="block flex-1 overflow-x-auto rounded bg-slate-950 px-3 py-2 text-xs text-slate-100" aria-label="New API key">{created.key}</code>
                <Button size="sm" onClick={() => navigator.clipboard?.writeText(created.key)}>Copy</Button>
              </div>
              <p className="mt-3 text-sm font-medium">Use it in a pipeline</p>
              <pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-3 text-xs">{`python3 scripts/modelassure_gate.py \\
  --api ${API_URL} \\
  --project ${created.project_id} \\
  --version <new build version> \\
  --endpoint <sandbox endpoint of the new build>
# exit 0 PASS · 1 FAIL · 2 INCONCLUSIVE · 3 error`}</pre>
              <p className="mt-2 text-xs text-slate-500">A PASS means the defined tests passed. It does not approve the release: a reviewer still records that decision here.</p>
              <Button size="sm" onClick={() => setCreated(null)}>Done</Button>
            </Card>
          )}
          <Card title="Keys" pad={false}>
            {keys.loading ? <Loading /> : (
              <Table head={["Key", "Project", "Status", "Last used", "Expires", ""]}
                     empty={!keys.data?.length && <div className="p-4"><Empty>No pipeline keys yet.</Empty></div>}>
                {keys.data?.map((k) => (
                  <tr key={k.id}>
                    <Td><div className="font-medium">{k.name}</div><Mono>{k.prefix}</Mono></Td>
                    <Td className="max-w-[12rem] truncate text-xs">{pname(k.project_id)}</Td>
                    <Td><Badge value={k.status === "ACTIVE" ? "PASS" : k.status === "REVOKED" ? "FAIL" : "INCONCLUSIVE"} label={k.status} /></Td>
                    <Td className="whitespace-nowrap text-xs text-slate-500">{k.last_used_at ? fmtDate(k.last_used_at) : "never"}</Td>
                    <Td className="whitespace-nowrap text-xs text-slate-500">{new Date(k.expires_at).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}</Td>
                    <Td>{k.status === "ACTIVE" && <Button size="sm" variant="danger" onClick={() => revoke(k)}>Revoke</Button>}</Td>
                  </tr>
                ))}
              </Table>
            )}
          </Card>
      </div>
    </>
  );
}
