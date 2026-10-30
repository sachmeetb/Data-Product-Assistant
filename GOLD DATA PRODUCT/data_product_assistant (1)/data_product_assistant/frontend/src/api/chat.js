const API_BASE = import.meta.env.VITE_API_URL !== undefined
  ? import.meta.env.VITE_API_URL
  : (typeof window !== 'undefined' && window.location.port === '5173' ? '' : 'http://localhost:8000');

// ── DIAGNOSTIC LOGGING ────────────────────────────────────────────────────
console.info('[api/chat] module loaded');
console.info('[api/chat] VITE_API_URL env =', import.meta.env.VITE_API_URL);
console.info('[api/chat] API_BASE in use  =', API_BASE);
console.info('[api/chat] Build MODE       =', import.meta.env.MODE);

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

export async function sendChatMessage(
  sessionId,
  message,
  { action, fileRefId, selectedEntities, selectedAttributes, domainName, industryId, contract, glossary } = {}
) {
  console.info('[api/chat] POST /chat', { sessionId, len: message?.length, action, selectedEntities });
  const res = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: sessionId,
      message,
      ...(action             ? { action }                                  : {}),
      ...(fileRefId          ? { file_ref_id: fileRefId }                  : {}),
      ...(selectedEntities   ? { selected_entities: selectedEntities }     : {}),
      ...(selectedAttributes ? { selected_attributes: selectedAttributes } : {}),
      ...(domainName         ? { domain_name: domainName }                 : {}),
      ...(industryId         ? { industry_id: industryId }                 : {}),
      ...(contract           ? { contract }                                : {}),
      ...(glossary           ? { glossary }                                : {}),
    }),
  });
  logResponseHeaders('chat', res);
  if (!res.ok) {
    const errText = await res.text();
    let detail = `Error ${res.status}`;
    try {
      const errJson = JSON.parse(errText);
      detail = errJson.detail || detail;
    } catch {
      detail = errText || detail;
    }
    throw new Error(detail);
  }
  const data = await res.json();
  return data;
}

export async function fetchSessionStructure(sessionId) {
  const sid = sessionId || 'default-gold-session';
  const res = await fetch(`${API_BASE}/v1/sessions/${encodeURIComponent(sid)}/structure`);
  if (!res.ok) throw new Error(`Failed to fetch structure: ${res.status}`);
  return res.json();
}

export async function addEntityToStructure(sessionId, payload) {
  const sid = sessionId || 'default-gold-session';
  const res = await fetch(`${API_BASE}/v1/sessions/${encodeURIComponent(sid)}/entities/add`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to add entity: ${res.status}`);
  return res.json();
}

export async function importEntityIntoStructure(sessionId, payload) {
  const sid = sessionId || 'default-gold-session';
  const res = await fetch(`${API_BASE}/v1/sessions/${encodeURIComponent(sid)}/entities/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to import entity: ${res.status}`);
  return res.json();
}

export async function saveSessionContract(sessionId, contract) {
  const sid = sessionId || 'default-gold-session';
  const res = await fetch(`${API_BASE}/v1/sessions/${encodeURIComponent(sid)}/contract`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ contract }),
  });
  if (!res.ok) throw new Error(`Failed to save contract: ${res.status}`);
  return res.json();
}

export async function saveSessionGlossary(sessionId, glossary, requirementUpdates = null) {
  const sid = sessionId || 'default-gold-session';
  const res = await fetch(`${API_BASE}/v1/sessions/${encodeURIComponent(sid)}/glossary`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      glossary,
      ...(requirementUpdates ? { requirement_updates: requirementUpdates } : {}),
    }),
  });
  if (!res.ok) throw new Error(`Failed to save glossary: ${res.status}`);
  return res.json();
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
