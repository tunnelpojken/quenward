// Small API client. The session cookie is HttpOnly, so JavaScript never sees it;
// the CSRF token is kept in memory only (not localStorage) and sent as a header.

let csrfToken = null;

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function messageFrom(status, data) {
  if (data && typeof data.detail === "string") return data.detail;
  if (status === 422) return "Some fields are invalid or too long.";
  if (status === 429) return "Too many attempts. Try again later.";
  return `Something went wrong (HTTP ${status}).`;
}

async function request(method, path, body, retry = true) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken;

  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
  });

  // After a server restart the CSRF token changes; fetch a fresh one and retry once.
  if (response.status === 403 && retry && method !== "GET" && csrfToken) {
    if (await getSession()) return request(method, path, body, false);
  }

  let data = null;
  if (response.status !== 204) {
    try {
      data = await response.json();
    } catch {
      data = null;
    }
  }
  if (!response.ok) throw new ApiError(response.status, messageFrom(response.status, data));
  return data;
}

export async function getSession() {
  try {
    const session = await request("GET", "/api/session");
    csrfToken = session.csrf_token;
    return session;
  } catch (error) {
    if (error.status === 401) {
      csrfToken = null;
      return null;
    }
    throw error;
  }
}

export async function setupStatus() {
  return (await request("GET", "/api/setup/status")).needs_setup;
}

export function setup(token, username, password) {
  return request("POST", "/api/setup", { token, username, password });
}

export async function login(username, password) {
  const session = await request("POST", "/api/login", { username, password });
  csrfToken = session.csrf_token;
  return session;
}

export async function logout() {
  try {
    await request("POST", "/api/logout");
  } finally {
    csrfToken = null;
  }
}

export function resetPassword(token, password) {
  return request("POST", "/api/password-reset", { token, password });
}
