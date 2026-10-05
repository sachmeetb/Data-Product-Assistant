import { useEffect, useState } from "react";
import api from "../api/client";

interface FileEntry {
  name: string;
  type: "file" | "directory";
  size: number | null;
  modified: number;
  path: string;
}

interface Props {
  projectId: number;
}

export default function ArtifactBrowser({ projectId }: Props) {
  const [currentPath, setCurrentPath] = useState("");
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const loadDir = async (path: string) => {
    setLoading(true);
    try {
      const res = await api.get(`/api/projects/${projectId}/artifacts`, { params: { path } });
      setFiles(res.data.files);
      setCurrentPath(path);
      setFileContent(null);
      setSelectedFile(null);
    } catch {
      setFiles([]);
    }
    setLoading(false);
  };

  const loadFile = async (path: string) => {
    try {
      const res = await api.get(`/api/projects/${projectId}/artifacts/content`, {
        params: { path },
        responseType: "text",
      });
      setFileContent(res.data);
      setSelectedFile(path);
    } catch {
      setFileContent("Error loading file.");
    }
  };

  useEffect(() => {
    loadDir("");
  }, [projectId]);

  const goUp = () => {
    const parts = currentPath.split("/").filter(Boolean);
    parts.pop();
    loadDir(parts.join("/"));
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <h3 style={{ margin: 0, color: "#334155" }}>Artifacts</h3>

      <div style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 13, color: "#64748b" }}>
        <span>/{currentPath}</span>
        {currentPath && (
          <button
            onClick={goUp}
            style={{
              padding: "2px 8px",
              borderRadius: 4,
              border: "1px solid #cbd5e1",
              background: "#fff",
              cursor: "pointer",
              fontSize: 12,
            }}
          >
            Up
          </button>
        )}
      </div>

      {loading ? (
        <div style={{ color: "#94a3b8" }}>Loading...</div>
      ) : files.length === 0 ? (
        <div style={{ color: "#94a3b8", fontSize: 13 }}>No files yet. Run a pipeline stage to generate artifacts.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {files.map((f) => (
            <div
              key={f.path}
              onClick={() => (f.type === "directory" ? loadDir(f.path) : loadFile(f.path))}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "6px 12px",
                borderRadius: 6,
                cursor: "pointer",
                backgroundColor: selectedFile === f.path ? "#e2e8f0" : "#fff",
                border: "1px solid #e2e8f0",
                fontSize: 13,
              }}
            >
              <span>
                {f.type === "directory" ? "📁 " : "📄 "}
                {f.name}
              </span>
              {f.size != null && (
                <span style={{ color: "#94a3b8", fontSize: 12 }}>
                  {f.size < 1024 ? `${f.size} B` : `${(f.size / 1024).toFixed(1)} KB`}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {fileContent !== null && (
        <pre
          style={{
            backgroundColor: "#1e293b",
            color: "#e2e8f0",
            padding: 16,
            borderRadius: 8,
            fontSize: 12,
            maxHeight: 400,
            overflow: "auto",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {fileContent}
        </pre>
      )}
    </div>
  );
}
