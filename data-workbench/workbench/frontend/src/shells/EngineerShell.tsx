import { useEffect, useMemo, useState } from "react";
import { Navigate } from "react-router-dom";
import { useRole, useSetRole } from "../RoleContext";
import { useAuth } from "../AuthContext";
import { useTheme } from "../theme";
import WorkbenchLayout, { type NavItem } from "./WorkbenchLayout";
import ShellRoleControl from "./ShellRoleControl";
import type { Role } from "../types";
import api from "../api/client";

const BASE_NAV: NavItem[] = [
  { to: "/engineer", label: "Projects", end: true },
  { to: "/engineer/incoming", label: "Incoming" },
  { to: "/engineer/intake", label: "Intake" },
  { to: "/engineer/marketplace", label: "Marketplace" },
  { to: "/engineer/semantic-recommender", label: "Concepts" },
  { to: "/engineer/connections", label: "Connections" },
  { to: "/engineer/settings", label: "Settings" },
];

const ENGINEER_ROLES: Role[] = [
  "Data Engineer",
  "Data Steward",
  "Data Quality Analyst",
  "Reviewer",
];

/**
 * Engineering Workbench shell. Filters the role selector to engineer-side
 * roles and applies the blue theme. If a user arrives here with role set to
 * "Data Product Owner" (e.g. they just came from the Product Workbench), we
 * shift them to "Data Engineer" so they can actually run stages.
 */
export default function EngineerShell() {
  const theme = useTheme();
  const role = useRole();
  const setRole = useSetRole();
  const { authEnabled, user } = useAuth();
  const incomingCount = usePolledIncomingCount();

  useEffect(() => {
    if (!ENGINEER_ROLES.includes(role)) {
      setRole("Data Engineer");
    }
    try {
      window.localStorage.setItem("workbench.preferred", "engineer");
    } catch {
      /* ignore storage errors */
    }
  }, [role, setRole]);

  const nav = useMemo<NavItem[]>(() => {
    return BASE_NAV.map((item) =>
      item.to === "/engineer/incoming" && incomingCount > 0
        ? { ...item, label: `Incoming (${incomingCount})` }
        : item
    );
  }, [incomingCount]);

  // Account-role gate: an owner account can't enter the Engineering Workbench.
  if (authEnabled && user && user.role !== "engineer") {
    return <Navigate to="/product" replace />;
  }

  return (
    <WorkbenchLayout
      theme={theme}
      homePath="/engineer"
      homeLabel="Data Workbench"
      navItems={nav}
      headerRight={<ShellRoleControl allowed={ENGINEER_ROLES} />}
    />
  );
}

/** Polls /api/product-requests?status=submitted every 15s so the nav
 *  badge reflects incoming work without a WebSocket round-trip. */
function usePolledIncomingCount(): number {
  const [count, setCount] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await api.get("/api/product-requests", { params: { status: "submitted" } });
        if (!cancelled) setCount((res.data?.requests || []).length);
      } catch {
        if (!cancelled) setCount(0);
      }
    };
    tick();
    const id = window.setInterval(tick, 15_000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);
  return count;
}
