import { useEffect } from "react";
import { Navigate } from "react-router-dom";
import { useSetRole } from "../RoleContext";
import { useAuth } from "../AuthContext";
import { useTheme } from "../theme";
import WorkbenchLayout, { type NavItem } from "./WorkbenchLayout";
import ShellRoleControl from "./ShellRoleControl";

const NAV: NavItem[] = [
  { to: "/product", label: "Home", end: true },
  { to: "/product/marketplace", label: "Marketplace" },
  { to: "/product/my-products", label: "My Products" },
  // Everything else lives as a card on the Product Workbench home
  // (Request New/Derived, Register Existing, Discovery, Data Product
  // Templates, Semantic Explorer, …) to keep the header lean. Gaps moved
  // into the Semantic Explorer.
];

/**
 * Product Workbench shell. Locks role to "Data Product Owner" while mounted
 * and applies the violet theme. Additional product-persona pages (My Products,
 * Propose New, Ingest) will slot into NAV in Phase 2.
 */
export default function ProductShell() {
  const theme = useTheme();
  const setRole = useSetRole();
  const { authEnabled, user } = useAuth();

  useEffect(() => {
    setRole("Data Product Owner");
    try {
      window.localStorage.setItem("workbench.preferred", "product");
    } catch {
      /* ignore storage errors */
    }
  }, [setRole]);

  // Account-role gate: an engineer account can't enter the Product Workbench.
  if (authEnabled && user && user.role !== "owner") {
    return <Navigate to="/engineer" replace />;
  }

  return (
    <WorkbenchLayout
      theme={theme}
      homePath="/product"
      homeLabel="Data Workbench"
      navItems={NAV}
      headerRight={<ShellRoleControl allowed={["Data Product Owner"]} />}
    />
  );
}
