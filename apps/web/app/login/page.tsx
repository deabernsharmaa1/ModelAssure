"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button, ErrorBox, Field, inputCls } from "@/components/ui";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const DEMO_USERS = [
  ["risk@tanjung.example", "Risk Manager"], ["engineer@tanjung.example", "Engineer"],
  ["analyst@tanjung.example", "Validation Analyst"], ["reviewer@tanjung.example", "Release Reviewer"],
  ["auditor@tanjung.example", "Auditor"], ["admin@tanjung.example", "Admin"],
];

export default function LoginPage() {
  const { login } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("risk@tanjung.example");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      router.replace("/");
    } catch (err) {
      setError(err as ApiError);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-[#0b2545] px-4">
      <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
        <h1 className="text-xl font-bold text-[#0b2545]">ModelAssure ReleaseGate</h1>
        <p className="mt-1 text-sm text-slate-600">Independent release assurance for financial decision systems.</p>
        <form onSubmit={submit} className="mt-6 space-y-4">
          <Field label="Email"><input className={inputCls} type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required /></Field>
          <Field label="Password"><input className={inputCls} type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required /></Field>
          <ErrorBox error={error} />
          <Button type="submit" variant="primary" disabled={busy} className="w-full justify-center">{busy ? "Signing in…" : "Sign in"}</Button>
        </form>
        <div className="mt-6 border-t border-slate-200 pt-4 text-xs text-slate-500">
          <div className="mb-2 font-medium text-slate-600">Demo accounts (development mode — fictional lender)</div>
          <div className="grid grid-cols-2 gap-1">
            {DEMO_USERS.map(([e, r]) => (
              <button key={e} type="button" onClick={() => setEmail(e)} className="rounded px-2 py-1 text-left hover:bg-slate-100">
                <span className="block text-slate-700">{r}</span><span className="block text-[10px]">{e}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
