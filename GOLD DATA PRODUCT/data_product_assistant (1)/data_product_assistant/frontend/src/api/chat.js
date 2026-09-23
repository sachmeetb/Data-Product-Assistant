const API_BASE = import.meta.env.VITE_API_URL || 'https://dataagents3-api.azurewebsites.net';

// ── DIAGNOSTIC LOGGING ────────────────────────────────────────────────────
// Temporary instrumentation to diagnose stale-backend deploys.
// Strip once we've confirmed which backend the deployed frontend talks to.
console.info('[api/chat] module loaded');
console.info('[api/chat] VITE_API_URL env =', import.meta.env.VITE_API_URL);
console.info('[api/chat] API_BASE in use  =', API_BASE);
console.info('[api/chat] Build MODE       =', import.meta.env.MODE);

// Reveal a server-identifying subset of response headers so we can tell which
// host responded (Azure adds x-azure-ref / x-ms-* / server etc.).
function logResponseHeaders(label, res) {
  const interesting = [
    'server', 'x-azure-ref', 'x-ms-request-id', 'x-powered-by',
    'date', 'content-length', 'content-type',
  ];
  const out = {};
  for (const h of interesting) {
    const v = res.headers.get(h);
    if (v) out[h] = v;
  }
  console.info(`[api/chat] ${label} ← ${res.status} ${res.url}`, out);
}
// ──────────────────────────────────────────────────────────────────────────

export async function sendChatMessage(sessionId, message, { action, fileRefId } = {}) {
  console.info('[api/chat] POST /chat', { sessionId, len: message?.length, action });
  const res = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      message,
      ...(action    ? { action }              : {}),
      ...(fileRefId ? { file_ref_id: fileRefId } : {}),
    }),
  });
  logResponseHeaders('chat', res);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
  return data;
}

export async function uploadFile(file, sessionId) {
  console.info('[api/chat] POST /upload', { name: file?.name, size: file?.size, sessionId });
  const form = new FormData();
  form.append('file', file);
  if (sessionId) form.append('session_id', sessionId);
  const res = await fetch(`${API_BASE}/upload`, { method: 'POST', body: form });
  logResponseHeaders('upload', res);
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || `Upload failed: ${res.status}`);
  return data;
}

export async function downloadFile(fileId, fileName) {
  console.info('[api/chat] GET /files/', fileId, '→', fileName);
  const res = await fetch(`${API_BASE}/files/${fileId}`);
  logResponseHeaders('files', res);
  if (!res.ok) throw new Error(`Download failed: ${res.status}`);
  const blob = await res.blob();
  console.info('[api/chat] downloaded blob', {
    fileName, size: blob.size, type: blob.type,
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
