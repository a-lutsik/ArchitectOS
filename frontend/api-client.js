// ArchitectOS API client: auth headers, fetch wrapper, health probe, SSE reader.
// ES module; `api()` is imported by nearly every other module.
import { t } from "./state.js";

const AUTH_TOKEN = (document.querySelector('meta[name="architectos-token"]') || {}).content || "";
function authHeaders(extra = {}) {
  return AUTH_TOKEN ? { "X-ArchitectOS-Token": AUTH_TOKEN, ...extra } : { ...extra };
}

async function api(path, options = {}) {
  const { timeoutMs, ...fetchOptions } = options;
  const controller = Number(timeoutMs) > 0 ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), Number(timeoutMs)) : null;
  let response;
  try {
    response = await fetch(path, {
      ...fetchOptions,
      signal: controller ? controller.signal : fetchOptions.signal,
      headers: authHeaders({ "Content-Type": "application/json", ...(fetchOptions.headers || {}) }),
    });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(t("error.requestTimeout"));
    }
    const message = error?.message || "network error";
    if (/failed to fetch|networkerror|load failed/i.test(message)) {
      throw new Error(t("error.serverUnreachable"));
    }
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
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

export { api, authHeaders, ensureServerOnline, readSseEvents };
