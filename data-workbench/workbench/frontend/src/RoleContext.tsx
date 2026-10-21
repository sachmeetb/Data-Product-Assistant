import { createContext, useContext } from "react";
import type { Role } from "./types";

interface RoleContextValue {
  role: Role;
  setRole: (role: Role) => void;
}

const noop = () => {};

export const RoleContext = createContext<RoleContextValue>({
  role: "Data Engineer",
  setRole: noop,
});

export function useRole(): Role {
  return useContext(RoleContext).role;
}

export function useSetRole(): (role: Role) => void {
  return useContext(RoleContext).setRole;
}
