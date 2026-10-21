import { useRole, useSetRole } from "../RoleContext";
import type { Role } from "../types";

interface Props {
  /** Roles this shell allows the user to switch between. When a single role
   *  is supplied the control renders as a non-interactive pill (the shell
   *  locks the role). */
  allowed: Role[];
}

export default function ShellRoleControl({ allowed }: Props) {
  const role = useRole();
  const setRole = useSetRole();

  if (allowed.length === 1) {
    return (
      <span
        style={{
          padding: "6px 12px",
          borderRadius: 6,
          fontSize: 13,
          fontWeight: 600,
          backgroundColor: "#f1f5f9",
          color: "#334155",
          border: "1px solid #cbd5e1",
        }}
        title="This role is fixed in the current workbench"
      >
        {allowed[0]}
      </span>
    );
  }

  return (
    <select
      value={allowed.includes(role) ? role : allowed[0]}
      onChange={(e) => setRole(e.target.value as Role)}
      style={{
        padding: "6px 12px",
        borderRadius: 6,
        border: "1px solid #cbd5e1",
        fontSize: 14,
        backgroundColor: "#f8fafc",
      }}
    >
      {allowed.map((r) => (
        <option key={r} value={r}>
          {r}
        </option>
      ))}
    </select>
  );
}
