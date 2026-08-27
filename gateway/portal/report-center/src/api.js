let csrf = "";

export function setCsrf(value) {
  csrf = value || "";
}

export async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) headers["X-CSRF-Token"] = csrf;
  const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  let payload = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload?.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}
