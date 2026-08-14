// ArchitectOS API client: auth headers, fetch wrapper, health probe, SSE reader.
// Classic script (shared global scope); `api()` is used by app.js and
// mcp_master_detail.js at call time.

const AUTH_TOKEN = (document.querySelector('meta[name="architectos-token"]') || {}).content || "";
function authHeaders(extra = {}) {
  return AUTH_TOKEN ? { "X-ArchitectOS-Token": AUTH_TOKEN, ...extra } : { ...extra };
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { ...options, headers: authHeaders({ "Content-Type": "application/json", ...(options.headers || {}) }) });
  } catch (error) {
    const message = error?.message || "network error";
    if (/failed to fetch|networkerror|load failed/i.test(message)) {
      throw new Error(t("error.serverUnreachable"));
    }
    throw error;
  }
  let payload = {};
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error(t("error.invalidResponse"));
  }
  if (!response.ok || payload.error) throw new Error(payload.error || payload.message || `Request failed: ${response.status}`);
  return payload;
}
async function ensureServerOnline() {
  try {
    await fetch("/api/health", { method: "GET", cache: "no-store", headers: authHeaders() });
    return true;
  } catch (error) {
    return false;
  }
}
// Shared SSE reader for the /api/*/stream endpoints: yields parsed `data:` events,
// skipping malformed frames instead of killing the stream.
async function* readSseEvents(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const line = frame.split("\n").find(item => item.startsWith("data:"));
      if (!line) continue;
      try {
        yield JSON.parse(line.slice(5).trim());
      } catch (_parseError) {
        continue;
      }
    }
  }
}
