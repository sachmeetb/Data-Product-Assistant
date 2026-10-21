import { createContext, useCallback, useContext, useEffect, useSyncExternalStore } from "react";
import type { ReactNode } from "react";
import api from "./api/client";
import {
  clearAuth,
  currentUserEmail,
  currentUserName,
  getAuthState,
  setAuthState,
  subscribeAuth,
} from "./authStore";
import type { AccountRole, AuthState, AuthUser } from "./authStore";

interface AuthContextValue extends AuthState {
  login: (email: string, password: string) => Promise<AuthUser>;
  logout: () => void;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const state = useSyncExternalStore(subscribeAuth, getAuthState);

  const refresh = useCallback(async () => {
    // Probe config first (unauthenticated) so we know whether to require login.
    try {
      const cfg = await api.get("/api/auth/config");
      const authEnabled = Boolean(cfg.data?.auth_enabled);
      let readOnly = Boolean(cfg.data?.read_only);
      let user = getAuthState().user;
      if (authEnabled && getAuthState().token) {
        try {
          const me = await api.get("/api/auth/me");
          user = { email: me.data.email, name: me.data.name, role: me.data.role as AccountRole };
          readOnly = Boolean(me.data.read_only);
        } catch {
          // Bad/expired token — drop it so RequireAuth routes to /login.
          clearAuth();
          user = null;
        }
      }
      setAuthState({ authEnabled, readOnly, user, ready: true });
    } catch {
      // Backend unreachable — assume auth off so local dev isn't wedged.
      setAuthState({ authEnabled: false, readOnly: false, ready: true });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (email: string, password: string): Promise<AuthUser> => {
    const res = await api.post("/api/auth/login", { email, password });
    const user: AuthUser = { email: res.data.email, name: res.data.name, role: res.data.role };
    setAuthState({ token: res.data.token, user });
    await refresh();
    return user;
  }, [refresh]);

  const logout = useCallback(() => {
    void api.post("/api/auth/logout").catch(() => {});
    clearAuth();
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within <AuthProvider>");
  return ctx;
}

/** Read-only flag derived from auth config / the current session. */
export function useReadOnly(): boolean {
  return useAuth().readOnly;
}

/** The logged-in user's email (falls back to the dev identity when auth off). */
export function useCurrentUserEmail(): string {
  const { user } = useAuth();
  return user?.email || currentUserEmail();
}

export function useCurrentUserName(): string {
  const { user } = useAuth();
  return user?.name || currentUserName();
}
