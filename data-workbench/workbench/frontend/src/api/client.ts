import axios from "axios";
import { clearAuth, getToken } from "../authStore";
import { emitToast } from "../lib/toastBus";

// API_BASE is env-overridable (VITE_API_BASE) for hosted deploys; falls back to
// the local-dev backend origin.
const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, "")
  || "http://localhost:8000";

const api = axios.create({ baseURL: API_BASE });

// Attach the session bearer token (if any) to every request.
api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers = config.headers ?? {};
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// On 401 → clear the session and bounce to /login. On a read-only 403 → surface
// a lightweight toast (the server 403 is the real guard; the UI just avoids a
// confusing silent failure).
api.interceptors.response.use(
  (r) => r,
  (error) => {
    const status = error?.response?.status;
    const data = error?.response?.data;
    if (status === 401) {
      clearAuth();
      if (window.location.pathname !== "/login") {
        window.location.assign("/login");
      }
    } else if (status === 403 && data?.read_only) {
      // Route through the toast bus (the provider owns rendering); the dedupeKey
      // collapses repeated 403s into one toast, as the old fixed-DOM-id did.
      emitToast({
        tone: "warning",
        message: data?.message || "Read-only instance — that action is disabled.",
        dedupeKey: "read-only",
      });
    }
    return Promise.reject(error);
  }
);

/** Build a WebSocket URL for a backend WS path, appending ?token= when a
 *  session token is present so the server can authenticate the socket. */
export function wsUrl(path: string): string {
  const base = API_BASE.replace(/^http/, "ws");
  const token = getToken();
  const sep = path.includes("?") ? "&" : "?";
  return token ? `${base}${path}${sep}token=${encodeURIComponent(token)}` : `${base}${path}`;
}

export default api;
export { API_BASE };
