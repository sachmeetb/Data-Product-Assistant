import { useState, useEffect } from "react";
import type { WSMessage } from "../types";

interface Props {
  question: WSMessage;
  onRespond: (questionId: string, value: string) => void;
}

export default function AgentQuestionRenderer({ question, onRespond }: Props) {
  const [textValue, setTextValue] = useState("");
  const [selectedValue, setSelectedValue] = useState<string>("");
  const [checkedValues, setCheckedValues] = useState<Set<string>>(new Set());
  const [timeLeft, setTimeLeft] = useState(question.timeout_seconds ?? 300);

  // Countdown timer
  useEffect(() => {
    if (question.message_type === "notification") return;
    const interval = setInterval(() => {
      setTimeLeft((prev) => {
        if (prev <= 1) {
          clearInterval(interval);
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => clearInterval(interval);
  }, [question.message_type]);

  const handleSubmit = () => {
    if (!question.question_id) return;

    if (question.message_type === "free_text") {
      onRespond(question.question_id, textValue);
    } else if (question.message_type === "yes_no") {
      // handled by button click directly
    } else if (question.message_type === "multiple_choice") {
      if (selectedValue) onRespond(question.question_id, selectedValue);
    } else if (question.message_type === "checklist") {
      if (checkedValues.size > 0) onRespond(question.question_id, Array.from(checkedValues).join(", "));
    }
  };

  const formatTime = (s: number) => {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${m}:${sec.toString().padStart(2, "0")}`;
  };

  // Notification: auto-dismiss style, no input needed
  if (question.message_type === "notification") {
    return (
      <div style={styles.container}>
        <div style={styles.badge}>Agent Notification</div>
        <div style={styles.prompt}>{question.prompt}</div>
        {question.context && <div style={styles.context}>{question.context}</div>}
      </div>
    );
  }

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <div style={styles.badge}>Agent Question</div>
        {timeLeft > 0 && (
          <div style={{
            ...styles.timer,
            color: timeLeft < 30 ? "#ef4444" : "#94a3b8",
          }}>
            {formatTime(timeLeft)}
          </div>
        )}
      </div>

      <div style={styles.prompt}>{question.prompt}</div>
      {question.context && <div style={styles.context}>{question.context}</div>}

      {/* Free text input */}
      {question.message_type === "free_text" && (
        <div style={styles.inputArea}>
          <textarea
            value={textValue}
            onChange={(e) => setTextValue(e.target.value)}
            placeholder="Type your response..."
            style={styles.textarea}
            rows={3}
          />
          <button
            onClick={handleSubmit}
            disabled={!textValue.trim()}
            style={{
              ...styles.button,
              ...styles.buttonPrimary,
              opacity: textValue.trim() ? 1 : 0.5,
            }}
          >
            Send Response
          </button>
        </div>
      )}

      {/* Yes/No */}
      {question.message_type === "yes_no" && (
        <div style={styles.buttonRow}>
          <button
            onClick={() => question.question_id && onRespond(question.question_id, "yes")}
            style={{ ...styles.button, ...styles.buttonPrimary }}
          >
            Yes
          </button>
          <button
            onClick={() => question.question_id && onRespond(question.question_id, "no")}
            style={{ ...styles.button, ...styles.buttonSecondary }}
          >
            No
          </button>
        </div>
      )}

      {/* Multiple choice */}
      {question.message_type === "multiple_choice" && question.options && (
        <div style={styles.inputArea}>
          <div style={styles.optionList}>
            {question.options.map((opt) => (
              <label
                key={opt.value}
                style={{
                  ...styles.optionLabel,
                  backgroundColor: selectedValue === opt.value ? "#1e3a5f" : "#0f172a",
                  borderColor: selectedValue === opt.value ? "#3b82f6" : "#334155",
                }}
              >
                <input
                  type="radio"
                  name={`q-${question.question_id}`}
                  value={opt.value}
                  checked={selectedValue === opt.value}
                  onChange={(e) => setSelectedValue(e.target.value)}
                  style={{ marginRight: 8 }}
                />
                <div>
                  <div style={{ fontWeight: 600 }}>{opt.label}</div>
                  {opt.description && (
                    <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>
                      {opt.description}
                    </div>
                  )}
                </div>
              </label>
            ))}
          </div>
          <button
            onClick={handleSubmit}
            disabled={!selectedValue}
            style={{
              ...styles.button,
              ...styles.buttonPrimary,
              opacity: selectedValue ? 1 : 0.5,
            }}
          >
            Submit Selection
          </button>
        </div>
      )}

      {/* Checklist (multi-select) */}
      {question.message_type === "checklist" && question.options && (
        <div style={styles.inputArea}>
          <div style={{ display: "flex", gap: 10, marginBottom: 4 }}>
            <span
              onClick={() => setCheckedValues(new Set(question.options!.map((o) => o.value)))}
              style={styles.selectLink}
            >
              Select all
            </span>
            <span
              onClick={() => setCheckedValues(new Set())}
              style={styles.selectLink}
            >
              Clear all
            </span>
          </div>
          <div style={{ ...styles.optionList, maxHeight: 240, overflowY: "auto" }}>
            {question.options.map((opt) => (
              <label
                key={opt.value}
                style={{
                  ...styles.optionLabel,
                  backgroundColor: checkedValues.has(opt.value) ? "#1e3a5f" : "#0f172a",
                  borderColor: checkedValues.has(opt.value) ? "#3b82f6" : "#334155",
                }}
              >
                <input
                  type="checkbox"
                  checked={checkedValues.has(opt.value)}
                  onChange={() => {
                    setCheckedValues((prev) => {
                      const next = new Set(prev);
                      if (next.has(opt.value)) next.delete(opt.value);
                      else next.add(opt.value);
                      return next;
                    });
                  }}
                  style={{ marginRight: 8 }}
                />
                <div>
                  <div style={{ fontWeight: 600 }}>{opt.label}</div>
                  {opt.description && (
                    <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 2 }}>
                      {opt.description}
                    </div>
                  )}
                </div>
              </label>
            ))}
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <button
              onClick={handleSubmit}
              disabled={checkedValues.size === 0}
              style={{
                ...styles.button,
                ...styles.buttonPrimary,
                opacity: checkedValues.size > 0 ? 1 : 0.5,
              }}
            >
              Confirm Selection ({checkedValues.size})
            </button>
            <span style={{ fontSize: 12, color: "#64748b" }}>
              {checkedValues.size} of {question.options.length} selected
            </span>
          </div>
        </div>
      )}

      {question.default_value && (
        <div style={styles.defaultHint}>
          Default if no response: {question.default_value}
        </div>
      )}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    margin: "8px 0",
    backgroundColor: "#0c1929",
    borderRadius: 8,
    padding: 14,
    borderLeft: "4px solid #3b82f6",
  },
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 8,
  },
  badge: {
    fontSize: 11,
    fontWeight: 700,
    textTransform: "uppercase" as const,
    letterSpacing: "0.05em",
    color: "#3b82f6",
    marginBottom: 6,
  },
  timer: {
    fontSize: 12,
    fontFamily: "monospace",
  },
  prompt: {
    fontSize: 14,
    color: "#e2e8f0",
    fontWeight: 600,
    lineHeight: 1.4,
    marginBottom: 4,
  },
  context: {
    fontSize: 12,
    color: "#94a3b8",
    marginBottom: 10,
    fontStyle: "italic",
  },
  inputArea: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 8,
    marginTop: 8,
  },
  textarea: {
    width: "100%",
    padding: 10,
    borderRadius: 6,
    border: "1px solid #334155",
    backgroundColor: "#0f172a",
    color: "#e2e8f0",
    fontFamily: "inherit",
    fontSize: 13,
    resize: "vertical" as const,
    outline: "none",
    boxSizing: "border-box" as const,
  },
  buttonRow: {
    display: "flex",
    gap: 8,
    marginTop: 10,
  },
  button: {
    padding: "8px 20px",
    borderRadius: 6,
    border: "none",
    fontWeight: 600,
    fontSize: 13,
    cursor: "pointer",
    transition: "all 0.15s",
  },
  buttonPrimary: {
    backgroundColor: "#3b82f6",
    color: "#fff",
  },
  buttonSecondary: {
    backgroundColor: "#334155",
    color: "#e2e8f0",
  },
  optionList: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 6,
  },
  optionLabel: {
    display: "flex",
    alignItems: "center",
    padding: "8px 12px",
    borderRadius: 6,
    border: "1px solid #334155",
    cursor: "pointer",
    color: "#e2e8f0",
    fontSize: 13,
    transition: "all 0.15s",
  },
  selectLink: {
    fontSize: 12,
    color: "#3b82f6",
    cursor: "pointer",
    fontWeight: 600,
  },
  defaultHint: {
    fontSize: 11,
    color: "#64748b",
    marginTop: 6,
    fontStyle: "italic",
  },
};
