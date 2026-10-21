import api from "../api/client";

/**
 * Download a zip (or any blob) from a Content-Disposition endpoint, preserving
 * the server-supplied filename. Shared by the serving-package download buttons
 * (Pipeline, ProjectDashboard) and the OKF exports (MarketplacePage).
 */
export async function downloadBlobZip(path: string, fallbackName: string): Promise<void> {
  const resp = await api.get(path, { responseType: "blob" });
  const disp = (resp.headers["content-disposition"] || "") as string;
  const match = disp.match(/filename="?([^";]+)"?/);
  const filename = match?.[1] || fallbackName;
  const url = URL.createObjectURL(resp.data as Blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
