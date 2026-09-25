const API_BASE = import.meta.env.VITE_API_URL !== undefined
  ? import.meta.env.VITE_API_URL
  : (typeof window !== 'undefined' && window.location.port === '5173' ? '' : 'http://localhost:8000');

export async function fetchDomainRegistry() {
  const res = await fetch(`${API_BASE}/catalog/domains`);
  if (!res.ok) throw new Error(`Failed to load domain registry: ${res.status}`);
  return res.json();
}

export async function fetchDomainFramework(domainName) {
  const res = await fetch(`${API_BASE}/catalog/domains/${encodeURIComponent(domainName)}`);
  if (!res.ok) throw new Error(`Failed to load domain '${domainName}': ${res.status}`);
  return res.json();
}
