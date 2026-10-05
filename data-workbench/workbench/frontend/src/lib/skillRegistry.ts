import { useEffect, useState } from "react";
import api from "../api/client";

export interface SkillInfo {
  slug: string;
  name: string;
  description: string;
}

let _cache: SkillInfo[] | null = null;
let _inflight: Promise<SkillInfo[]> | null = null;

async function fetchSkills(): Promise<SkillInfo[]> {
  if (_cache) return _cache;
  if (_inflight) return _inflight;
  _inflight = api
    .get("/api/skills")
    .then((res) => {
      const list = (res.data?.skills || []) as SkillInfo[];
      _cache = list;
      return list;
    })
    .catch(() => {
      _cache = [];
      return [];
    })
    .finally(() => {
      _inflight = null;
    });
  return _inflight;
}

/** Hook: returns a getInfo() lookup. The lookup is stable across renders;
 *  consumers don't need to memoise anything. Skills are fetched once per
 *  page lifetime and shared via a module-level cache. */
export function useSkillRegistry(): { getInfo: (name: string) => SkillInfo | null } {
  const [list, setList] = useState<SkillInfo[]>(_cache ?? []);

  useEffect(() => {
    let cancelled = false;
    fetchSkills().then((l) => {
      if (!cancelled) setList(l);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return {
    getInfo: (name: string) => {
      if (!name) return null;
      const lower = name.toLowerCase();
      return list.find((s) => s.slug.toLowerCase() === lower || s.name.toLowerCase() === lower) ?? null;
    },
  };
}

export function _resetSkillRegistryCache(): void {
  _cache = null;
  _inflight = null;
}
