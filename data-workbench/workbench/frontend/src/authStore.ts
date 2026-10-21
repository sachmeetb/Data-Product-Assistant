// Module-level auth store shared by React (AuthContext via useSyncExternalStore)
// AND non-React code (the axios interceptor, WebSocket URL builder, and the
// legacy module-scope identity constants). One source of truth, persisted to
// localStorage["workbench.auth"] so a reload keeps the session.

export type AccountRole = "owner" | "engineer";

export interface AuthUser {
  email: string;
  name: string;
  role: AccountRole;
}

export interface AuthState {
  token: string | null;
  user: AuthUser | null;
  readOnly: boolean;
  authEnabled: boolean;
  // false until GET /api/auth/config has resolved, so the app can show a
  // brief boot state instead of flashing /login.
  ready: boolean;
}

const STORAGE_KEY = "workbench.auth";

// Fallback identity when auth is DISABLED (local dev) — preserves the old
// hardcoded-email behaviour so provenance/ownership still resolve to a person.
const DEV_FALLBACK_EMAIL = "niel.c.eyde@accenture.com";
const DEV_FALLBACK_NAME = "Niel Eyde";

function load(): AuthState {
  let token: string | null = null;
  let user: AuthUser | null = null;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      token = parsed.token ?? null;
      user = parsed.user ?? null;
    }
  } catch {
    /* ignore storage / parse errors */
  }
  return { token, user, readOnly: false, authEnabled: false, ready: false };
}

let state: AuthState = load();
const listeners = new Set<() => void>();

export function getAuthState(): AuthState {
  return state;
}

export function setAuthState(patch: Partial<AuthState>): void {
  state = { ...state, ...patch };
  try {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ token: state.token, user: state.user })
    );
  } catch {
    /* ignore */
  }
  listeners.forEach((l) => l());
}

export function subscribeAuth(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function clearAuth(): void {
  setAuthState({ token: null, user: null });
}

// ── Non-React accessors ──────────────────────────────────────────────────
export function getToken(): string | null {
  return state.token;
}

/** The current user's email — the logged-in user, or the dev fallback when
 *  auth is disabled. Replaces the scattered hardcoded CURRENT_USER_EMAIL. */
export function currentUserEmail(): string {
  return state.user?.email || DEV_FALLBACK_EMAIL;
}

export function currentUserName(): string {
  return state.user?.name || DEV_FALLBACK_NAME;
}

export function isReadOnly(): boolean {
  return state.readOnly;
}
