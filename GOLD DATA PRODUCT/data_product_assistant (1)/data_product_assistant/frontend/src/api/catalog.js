const API_BASE = import.meta.env.VITE_API_URL !== undefined
  ? import.meta.env.VITE_API_URL
  : (typeof window !== 'undefined' && window.location.port === '5173' ? '' : 'http://localhost:8000');

export async function fetchIndustries() {
  const res = await fetch(`${API_BASE}/catalog/industries`);
  if (!res.ok) throw new Error(`Failed to load industries: ${res.status}`);
  return res.json();
}

export async function fetchIndustrySubtypes(industryId) {
  const res = await fetch(`${API_BASE}/catalog/industries/${encodeURIComponent(industryId)}/subtypes`);
  if (!res.ok) throw new Error(`Failed to load subtypes for '${industryId}': ${res.status}`);
  return res.json();
}

export async function fetchIndustryDomains(industryId) {
  const res = await fetch(`${API_BASE}/catalog/industries/${encodeURIComponent(industryId)}/domains`);
  if (!res.ok) throw new Error(`Failed to load domains for '${industryId}': ${res.status}`);
  return res.json();
}

export async function fetchDomainRegistry(industry = '') {
  const q = industry ? `?industry=${encodeURIComponent(industry)}` : '';
  const res = await fetch(`${API_BASE}/catalog/domains${q}`);
  if (!res.ok) throw new Error(`Failed to load domain registry: ${res.status}`);
  return res.json();
}

export async function fetchDomainFramework(domainName) {
  const res = await fetch(`${API_BASE}/catalog/domains/${encodeURIComponent(domainName)}`);
  if (!res.ok) throw new Error(`Failed to load domain '${domainName}': ${res.status}`);
  return res.json();
}

export async function fetchNavigatorTree() {
  const res = await fetch(`${API_BASE}/catalog/navigator`);
  if (!res.ok) throw new Error(`Failed to load navigator tree: ${res.status}`);
  return res.json();
}

export async function fetchDomainEntities(domainName) {
  const res = await fetch(`${API_BASE}/catalog/domains/${encodeURIComponent(domainName)}/entities`);
  if (!res.ok) throw new Error(`Failed to load entities for '${domainName}': ${res.status}`);
  return res.json();
}

export async function fetchSourceModels(industry = '') {
  const q = industry ? `?industry=${encodeURIComponent(industry)}` : '';
  const res = await fetch(`${API_BASE}/catalog/source-models${q}`);
  if (!res.ok) throw new Error(`Failed to load source models: ${res.status}`);
  return res.json();
}

export async function submitNavigatorSelection(payload) {
  const res = await fetch(`${API_BASE}/catalog/navigator/select`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Navigator selection failed: ${res.status}`);
  return res.json();
}
