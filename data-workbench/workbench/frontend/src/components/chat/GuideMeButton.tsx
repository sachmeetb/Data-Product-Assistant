import { productTheme } from "../../theme";

interface Props {
  label?: string;
  onClick: () => void;
  /** Small / inline is the default — a lightweight label the user can click
   *  next to a form field. Pass `prominent` for a bigger call-to-action
   *  (e.g. the idea textarea where there's no competing control). */
  prominent?: boolean;
  disabled?: boolean;
}

/**
 * "Guide me" affordance. Click opens the product authoring chat drawer
 * pre-filled with a context-specific template; the parent component owns
 * the drawer state and provides the template via a
 * :class:`GuideMeRequest` passed to :class:`ProductChatPanel`.
 */
export default function GuideMeButton({ label = "Guide me", onClick, prominent = false, disabled = false }: Props) {
  const base = {
    border: `1px solid ${productTheme.accent}`,
    borderRadius: 6,
    color: productTheme.accent,
    backgroundColor: "#fff",
    cursor: disabled ? "not-allowed" : "pointer",
    fontWeight: 600,
    transition: "background-color 0.12s",
  } as const;
  const sized = prominent
    ? { ...base, padding: "6px 14px", fontSize: 13 }
    : { ...base, padding: "2px 10px", fontSize: 11 };
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{ ...sized, opacity: disabled ? 0.5 : 1 }}
      title="Ask the assistant for help with this field"
    >
      ✨ {label}
    </button>
  );
}
