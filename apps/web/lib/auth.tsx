"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, getToken, setToken } from "./api";
import type { Me, Role } from "./types";

interface AuthState {
  me: Me | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  has: (...roles: Role[]) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    if (!getToken()) { setMe(null); setLoading(false); return; }
    try { setMe(await api.get<Me>("/v1/me")); } catch { setMe(null); } finally { setLoading(false); }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const login = async (email: string, password: string) => {
    const r = await api.post<{ access_token: string }>("/v1/auth/login", { email, password });
    setToken(r.access_token);
    setLoading(true);
    await refresh();
  };
  const logout = () => { setToken(null); setMe(null); window.location.href = "/login"; };
  const has = (...roles: Role[]) => !!me && roles.some((r) => me.roles.includes(r));

  return <AuthContext.Provider value={{ me, loading, login, logout, has }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

// Mirrors the API permission matrix (the API is the authority; this only hides unusable controls).
export const CAN = {
  createProject: ["ADMIN", "RISK_MANAGER"] as Role[],
  manageSystems: ["ADMIN", "ENGINEER"] as Role[],
  editPolicy: ["ADMIN", "RISK_MANAGER"] as Role[],
  approvePolicy: ["RISK_MANAGER", "REVIEWER"] as Role[],
  createSuite: ["ADMIN", "RISK_MANAGER", "ANALYST", "ENGINEER"] as Role[],
  createRun: ["ADMIN", "RISK_MANAGER", "ANALYST", "ENGINEER"] as Role[],
  updateFinding: ["ADMIN", "RISK_MANAGER", "ANALYST", "ENGINEER"] as Role[],
  acceptRisk: ["RISK_MANAGER"] as Role[],
  closeFinding: ["RISK_MANAGER", "ADMIN"] as Role[],
  review: ["REVIEWER"] as Role[],
  audit: ["AUDITOR", "ADMIN", "RISK_MANAGER"] as Role[],
};
