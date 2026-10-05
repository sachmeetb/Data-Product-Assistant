import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import api from "../api/client";
import { useAuth } from "../AuthContext";
import type { Theme } from "../theme";

export interface NavItem {
  to: string;
  label: string;
  end?: boolean;
}

interface Props {
  theme: Theme;
  homePath: string;
  homeLabel: string;
  navItems: NavItem[];
  /** Component rendered in the top-right (typically a role selector or a
   *  locked role pill). */
  headerRight?: ReactNode;
}

export default function WorkbenchLayout({
  theme,
  homePath,
  homeLabel,
  navItems,
  headerRight,
}: Props) {
  const [neo4jBrowserUrl, setNeo4jBrowserUrl] = useState("");
  const { readOnly } = useAuth();

  useEffect(() => {
    api
      .get("/api/settings")
      .then((res) => {
        setNeo4jBrowserUrl(res.data.neo4j_browser_url || "");
      })
      .catch(() => {});
  }, []);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", fontFamily: "system-ui, sans-serif" }}>
      {readOnly && (
        <div style={{
          backgroundColor: "#7c2d12", color: "#fff", textAlign: "center",
          padding: "6px 12px", fontSize: 13, fontWeight: 600, letterSpacing: 0.2,
        }}>
          Read-only demo instance — actions are disabled.
        </div>
      )}
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "12px 24px",
          borderBottom: "1px solid #e2e8f0",
          backgroundColor: theme.headerBg,
          color: theme.headerFg,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
          <Link to={homePath} style={{ color: theme.headerFg, textDecoration: "none", fontWeight: 700, fontSize: 18 }}>
            {homeLabel}
          </Link>
          <span
            style={{
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: 0.4,
              textTransform: "uppercase",
              padding: "3px 8px",
              borderRadius: 4,
              backgroundColor: theme.badgeBg,
              color: theme.badgeFg,
            }}
          >
            {theme.label}
          </span>
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              style={({ isActive }) => ({
                color: isActive ? theme.headerFg : theme.headerMuted,
                textDecoration: "none",
                fontSize: 14,
                fontWeight: isActive ? 600 : 400,
              })}
            >
              {item.label}
            </NavLink>
          ))}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          {neo4jBrowserUrl && (
            <a
              href={neo4jBrowserUrl}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                color: "#a5f3fc",
                textDecoration: "none",
                fontSize: 13,
                fontWeight: 600,
                padding: "4px 10px",
                borderRadius: 5,
                border: "1px solid #334155",
                backgroundColor: "#0f172a",
              }}
            >
              Neo4j Browser
            </a>
          )}
          <SwitchMenu theme={theme} />
          {headerRight}
        </div>
      </header>

      <main style={{ flex: 1, overflow: "auto", padding: 24, backgroundColor: theme.bgTint }}>
        <Outlet />
      </main>
    </div>
  );
}

function SwitchMenu({ theme }: { theme: Theme }) {
  const navigate = useNavigate();
  const { authEnabled, user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  // With auth on, an account can only enter the shell its role allows:
  // owner → Product, engineer → Engineering. So the cross-shell switch is only
  // offered when auth is disabled (dev) or... never for a bound account.
  const canSwitchShell = !authEnabled || !user;

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  const otherKind = theme.kind === "product" ? "engineer" : "product";
  const otherLabel = theme.kind === "product" ? "Engineering" : "Product";

  const goOther = () => {
    setOpen(false);
    navigate(otherKind === "product" ? "/product" : "/engineer");
  };

  const goHome = () => {
    setOpen(false);
    try {
      window.localStorage.removeItem("workbench.preferred");
    } catch {
      /* ignore storage errors */
    }
    navigate("/");
  };

  return (
    <div ref={wrapperRef} style={{ position: "relative" }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title="Switch workbench"
        style={{
          color: theme.headerMuted,
          background: "transparent",
          fontSize: 12,
          padding: "4px 8px",
          borderRadius: 4,
          border: `1px solid ${theme.headerMuted}`,
          cursor: "pointer",
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          fontFamily: "inherit",
        }}
      >
        <span>Switch</span>
        <span aria-hidden style={{ fontSize: 9, opacity: 0.8 }}>▾</span>
      </button>
      {open && (
        <div
          role="menu"
          style={{
            position: "absolute",
            top: "calc(100% + 6px)",
            right: 0,
            minWidth: 200,
            backgroundColor: "#fff",
            borderRadius: 6,
            border: "1px solid #e2e8f0",
            boxShadow: "0 6px 20px rgba(15,23,42,0.15)",
            padding: 4,
            zIndex: 50,
          }}
        >
          {canSwitchShell && <SwitchMenuItem onClick={goOther} label={`Switch to ${otherLabel}`} />}
          {canSwitchShell && <SwitchMenuItem onClick={goHome} label="Home (choose workbench)" />}
          {authEnabled && user && (
            <SwitchMenuItem
              onClick={() => { setOpen(false); logout(); navigate("/login", { replace: true }); }}
              label={`Sign out (${user.email})`}
            />
          )}
        </div>
      )}
    </div>
  );
}

function SwitchMenuItem({ onClick, label }: { onClick: () => void; label: string }) {
  const [hover, setHover] = useState(false);
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        padding: "8px 10px",
        borderRadius: 4,
        border: "none",
        backgroundColor: hover ? "#f1f5f9" : "transparent",
        color: "#0f172a",
        fontSize: 13,
        fontFamily: "inherit",
        cursor: "pointer",
      }}
    >
      {label}
    </button>
  );
}
