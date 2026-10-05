import { useRef } from "react";
import { useToast } from "../dialogContext";

export interface AttachmentDraft {
  filename: string;
  size: number;
  mime: string;
  content_base64: string;
}

interface Props {
  attachments: AttachmentDraft[];
  setAttachments: React.Dispatch<React.SetStateAction<AttachmentDraft[]>>;
  /** Accent color used for the paperclip border + remove buttons. Engineering
   *  uses #2563eb-ish blue; Product uses violet. */
  accent?: string;
  disabled?: boolean;
}

const ACCEPT = ".txt,.json,.csv,text/plain,application/json,text/csv";
const MAX_BYTES = 2 * 1024 * 1024;

/**
 * Compact "paperclip + chip-list" attachment row. Sits inside the chat
 * composer, above the textarea/Send row. Reads files via FileReader as
 * base64 and hands them off to the parent's `setAttachments` setter; the
 * parent includes the array on its next WS send_message and clears the
 * list once the message is dispatched.
 */
export default function AttachmentBar({
  attachments,
  setAttachments,
  accent = "#3b82f6",
  disabled = false,
}: Props) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const toast = useToast();

  const onPick = () => {
    if (disabled) return;
    fileInputRef.current?.click();
  };

  const onChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = ""; // allow re-selecting the same file later
    const accepted: AttachmentDraft[] = [];
    for (const file of files) {
      if (file.size > MAX_BYTES) {
        toast({ tone: "warning", message: `${file.name}: too large (max 2 MB)` });
        continue;
      }
      const b64 = await readFileAsBase64(file);
      if (b64 === null) continue;
      accepted.push({
        filename: file.name,
        size: file.size,
        mime: file.type || "",
        content_base64: b64,
      });
    }
    if (accepted.length > 0) {
      setAttachments((prev) => [...prev, ...accepted]);
    }
  };

  const remove = (idx: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  };

  return (
    <>
      <input
        ref={fileInputRef}
        type="file"
        accept={ACCEPT}
        multiple
        onChange={onChange}
        style={{ display: "none" }}
      />
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <button
          type="button"
          onClick={onPick}
          disabled={disabled}
          title="Attach a .txt, .json, or .csv file (max 2 MB)"
          aria-label="Attach file"
          style={{
            width: 26,
            height: 26,
            border: `1px solid ${disabled ? "#cbd5e1" : accent}`,
            borderRadius: 6,
            background: "#fff",
            color: disabled ? "#cbd5e1" : accent,
            cursor: disabled ? "not-allowed" : "pointer",
            fontSize: 14,
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 0,
            lineHeight: 1,
          }}
        >
          📎
        </button>
        {attachments.map((a, i) => (
          <span
            key={`${a.filename}-${i}`}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "2px 6px",
              borderRadius: 999,
              backgroundColor: "#f1f5f9",
              color: "#334155",
              border: "1px solid #cbd5e1",
              fontSize: 11,
              maxWidth: 180,
            }}
            title={`${a.filename} · ${formatSize(a.size)}`}
          >
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {a.filename}
            </span>
            <button
              type="button"
              onClick={() => remove(i)}
              aria-label={`Remove ${a.filename}`}
              title="Remove"
              style={{
                border: "none",
                background: "transparent",
                color: "#64748b",
                cursor: "pointer",
                padding: 0,
                fontSize: 12,
                lineHeight: 1,
              }}
            >
              ×
            </button>
          </span>
        ))}
      </div>
    </>
  );
}

function readFileAsBase64(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        resolve(null);
        return;
      }
      // Strip the "data:<mime>;base64," prefix
      const idx = result.indexOf(",");
      resolve(idx >= 0 ? result.slice(idx + 1) : result);
    };
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
