"use client";

import { useState } from "react";
import { Badge, Button, Card, ErrorBox, Loading, Mono, PageHeader, Select, Table, Td, useApi } from "@/components/ui";
import { ApiError, api, qs } from "@/lib/api";
import { fmtDate, short } from "@/lib/format";
import type { AuditEvent } from "@/lib/types";

const ACTIONS = ["access.denied", "auth.login", "connection.create", "connection.verify", "finding.update", "policy_set.approve", "policy_set.create",
  "policy_set.submit", "project.create", "release.review", "report.download", "report.generate", "run.complete", "run.create", "system_version.register", "test_suite.create"];

export default function AuditPage() {
  const [action, setAction] = useState("");
  const [offset, setOffset] = useState(0);
  const events = useApi(() => api.get<{ total: number; items: AuditEvent[] }>(`/v1/audit-events${qs({ action, limit: 100, offset })}`), [action, offset]);
  const [verify, setVerify] = useState<{ valid: boolean; checked: number; head?: string; problem?: string } | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  return (
    <>
      <PageHeader title="Audit trail" subtitle="Append-only and hash-chained: every material action is recorded and any alteration is detectable."
                  actions={<Button variant="primary" onClick={async () => { setError(null); try { setVerify(await api.get("/v1/audit-events/verify")); } catch (e) { setError(e as ApiError); } }}>Verify chain integrity</Button>} />
      <ErrorBox error={error ?? events.error} />
      {verify && (
        <div className={`mb-4 rounded-md border px-4 py-3 text-sm ${verify.valid ? "border-green-300 bg-green-50 text-green-900" : "border-red-300 bg-red-50 text-red-900"}`} role="status">
          {verify.valid ? <>✔ Chain intact: {verify.checked} events verified. Head <Mono>{short(verify.head, 16)}</Mono></> : <>✖ Integrity failure after {verify.checked} events: {verify.problem}</>}
        </div>
      )}
      <div className="mb-3 max-w-xs"><Select aria-label="Action" value={action} onChange={(v) => { setOffset(0); setAction(v); }} placeholder="All actions" options={ACTIONS.map((a) => ({ value: a, label: a }))} /></div>
      <Card pad={false}>
        {events.loading && !events.data ? <Loading /> : (
          <Table head={["#", "Time", "Actor", "Action", "Resource", "Outcome", "Details", "Hash"]}>
            {events.data?.items.map((e) => (
              <tr key={e.id}>
                <Td className="tabular-nums text-xs">{e.seq}</Td><Td className="whitespace-nowrap text-xs">{fmtDate(e.timestamp)}</Td>
                <Td className="text-xs">{e.actor_label}</Td><Td><Mono>{e.action}</Mono></Td>
                <Td className="text-xs">{e.resource_type}<div className="text-slate-400">{short(e.resource_id, 8)}</div></Td>
                <Td><Badge value={e.outcome === "SUCCESS" ? "PASS" : e.outcome === "DENIED" ? "FAIL" : "INCONCLUSIVE"} label={e.outcome} /></Td>
                <Td className="max-w-sm"><details><summary className="cursor-pointer text-xs text-blue-800">view</summary><pre className="whitespace-pre-wrap text-[11px]">{JSON.stringify(e.details, null, 2)}</pre></details></Td>
                <Td><Mono>{short(e.hash, 8)}</Mono></Td>
              </tr>
            ))}
          </Table>
        )}
        {events.data && events.data.total > 100 && (
          <div className="flex items-center justify-between border-t border-slate-200 px-3 py-2 text-xs">
            <span>{offset + 1}–{Math.min(offset + 100, events.data.total)} of {events.data.total}</span>
            <div className="flex gap-2"><Button size="sm" disabled={!offset} onClick={() => setOffset(offset - 100)}>Newer</Button>
              <Button size="sm" disabled={offset + 100 >= events.data.total} onClick={() => setOffset(offset + 100)}>Older</Button></div>
          </div>
        )}
      </Card>
    </>
  );
}
