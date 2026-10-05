import type { SkillInfo } from "../../lib/skillRegistry";

export interface ToolUseLabel {
  /** Bold-displayed primary line. */
  label: string;
  /** Optional smaller subline. Markdown-friendly but plain-text expected. */
  description: string | null;
}

const SKILL_SCRIPT_RE = /\/skills\/([\w-]+)\/scripts\/([\w-]+)\.py/;

function humanize(scriptName: string): string {
  return scriptName
    .replace(/[_-]+/g, " ")
    .replace(/\.py$/i, "")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function truncate(s: string, n: number): string {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s;
}

function pickInputBlurb(input: unknown): string {
  if (!input || typeof input !== "object") return "";
  const rec = input as Record<string, unknown>;
  const val = rec.command ?? rec.query ?? rec.file_path ?? rec.pattern ?? rec.path ?? rec.skill_name ?? "";
  const s = typeof val === "string" ? val : JSON.stringify(val);
  return truncate(s, 200);
}

/** Turn a raw tool_use event into a friendly { label, description } pair.
 *  Used by both ChatPanel (Engineering) and ProductChatPanel (PO) so the
 *  rendering is identical. */
export function formatToolUse(
  tool: string | undefined,
  input: unknown,
  getInfo: (name: string) => SkillInfo | null,
): ToolUseLabel {
  const t = (tool || "tool").trim();

  if (t === "Skill") {
    // SDK passes `{ skill_name: "..." }` (or sometimes nested under
    // `name` — check both, fall back to whatever string we can find).
    let skillName = "";
    if (input && typeof input === "object") {
      const rec = input as Record<string, unknown>;
      skillName = String(rec.skill_name ?? rec.name ?? "").trim();
    } else if (typeof input === "string") {
      skillName = input.trim();
    }
    const info = getInfo(skillName);
    if (info) {
      return {
        label: `Loading skill: ${info.name}`,
        description: info.description || null,
      };
    }
    return {
      label: skillName ? `Loading skill: ${skillName}` : "Loading skill",
      description: null,
    };
  }

  if (t === "Bash") {
    const command = (() => {
      if (input && typeof input === "object") {
        const c = (input as Record<string, unknown>).command;
        if (typeof c === "string") return c;
      }
      return "";
    })();
    const m = command.match(SKILL_SCRIPT_RE);
    if (m) {
      const slug = m[1];
      const script = m[2];
      const info = getInfo(slug);
      const skillLabel = info?.name || slug;
      return {
        label: `Running ${skillLabel}: ${humanize(script)}`,
        description: info?.description || null,
      };
    }
    return {
      label: "Running shell command",
      description: command ? truncate(command, 160) : null,
    };
  }

  // Read / Write / Edit / Grep / Glob / etc. → tool name + a hint of what.
  const blurb = pickInputBlurb(input);
  return {
    label: t,
    description: blurb || null,
  };
}
