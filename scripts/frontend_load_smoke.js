#!/usr/bin/env node
// No-browser load-order smoke test for the classic-script frontend.
// Parses frontend/index.html for the ordered `<script src="/x.js">` list,
// concatenates those files, and executes them in a single vm context with
// browser stubs. Exits non-zero (printing the offending concat line) if any
// script throws at top level; prints OK otherwise. No dependencies.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..");
const FRONTEND = path.join(ROOT, "frontend");
const html = fs.readFileSync(path.join(FRONTEND, "index.html"), "utf8");

const srcs = [];
const re = /<script\s+src="\/([^"]+\.js)"\s*>\s*<\/script>/g;
let m;
while ((m = re.exec(html)) !== null) srcs.push(m[1]);
if (srcs.length === 0) {
  console.error("No <script src> tags found in index.html");
  process.exit(1);
}

// Build one concatenated source and remember which file each line came from.
const lineOwners = [];
const parts = [];
for (const src of srcs) {
  const p = path.join(FRONTEND, src);
  if (!fs.existsSync(p)) {
    console.error(`Missing script file: ${src}`);
    process.exit(1);
  }
  const body = fs.readFileSync(p, "utf8");
  parts.push(`// ===== ${src} =====`);
  lineOwners.push(src);
  for (const line of body.split("\n")) {
    parts.push(line);
    lineOwners.push(src);
  }
}
const concat = parts.join("\n");

// Deep chainable stub used for `document` and unknown globals/DOM nodes.
function makeStub() {
  const target = function () {
    return proxy;
  };
  const proxy = new Proxy(target, {
    get(_t, prop) {
      if (prop === Symbol.toPrimitive) {
        return hint => (hint === "number" ? 0 : "null");
      }
      if (prop === "toString" || prop === "valueOf") return () => "null";
      if (prop === "length") return 0;
      if (prop === Symbol.iterator) {
        return function* () {};
      }
      if (prop === "then") return undefined; // not a thenable
      return proxy;
    },
    apply() {
      return proxy;
    },
    construct() {
      return proxy;
    },
    set() {
      return true;
    },
    has() {
      return true;
    },
  });
  return proxy;
}

const stub = makeStub();

const documentStub = new Proxy(
  {},
  {
    get(_t, prop) {
      if (prop === "getElementById" || prop === "querySelector" || prop === "createElement") {
        return () => stub;
      }
      if (
        prop === "querySelectorAll" ||
        prop === "getElementsByClassName" ||
        prop === "getElementsByTagName" ||
        prop === "getElementsByName"
      ) {
        return () => [];
      }
      if (prop === "addEventListener" || prop === "removeEventListener") {
        return () => {};
      }
      if (prop === "readyState") return "complete";
      if (prop === "body" || prop === "documentElement" || prop === "head") return stub;
      if (prop === "cookie") return "";
      return stub;
    },
    set() {
      return true;
    },
  }
);

const storageStub = {
  _d: {},
  getItem(k) {
    return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null;
  },
  setItem(k, v) {
    this._d[k] = String(v);
  },
  removeItem(k) {
    delete this._d[k];
  },
  clear() {
    this._d = {};
  },
  key() {
    return null;
  },
  get length() {
    return Object.keys(this._d).length;
  },
};

const ctx = {};
Object.assign(ctx, {
  document: documentStub,
  localStorage: storageStub,
  sessionStorage: storageStub,
  console,
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: () => 0,
  clearInterval: () => {},
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }),
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}), text: () => Promise.resolve("") }),
  location: { href: "http://localhost/", origin: "http://localhost", pathname: "/", search: "", hash: "", reload() {}, assign() {}, replace() {} },
  navigator: { language: "en", languages: ["en"], userAgent: "node", clipboard: { writeText: () => Promise.resolve() } },
  history: { pushState() {}, replaceState() {}, back() {}, forward() {} },
  alert: () => {},
  confirm: () => false,
  prompt: () => null,
  URL,
  URLSearchParams,
  FormData: class FormData { append() {} get() { return null; } getAll() { return []; } has() { return false; } },
  CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; Object.assign(this, init); } },
  Event: class Event { constructor(type) { this.type = type; } },
  EventSource: class EventSource { constructor() {} addEventListener() {} close() {} },
  WebSocket: class WebSocket { constructor() {} send() {} close() {} addEventListener() {} },
  MutationObserver: class MutationObserver { observe() {} disconnect() {} takeRecords() { return []; } },
  ResizeObserver: class ResizeObserver { observe() {} unobserve() {} disconnect() {} },
  IntersectionObserver: class IntersectionObserver { observe() {} unobserve() {} disconnect() {} },
  getComputedStyle: () => stub,
});
ctx.window = ctx;
ctx.window.addEventListener = () => {};
ctx.window.removeEventListener = () => {};
ctx.self = ctx;
ctx.globalThis = ctx;

// Unknown-global fallback: any bare identifier read that is not defined on ctx
// resolves to the deep stub instead of throwing ReferenceError.
const sandbox = new Proxy(ctx, {
  get(t, prop) {
    if (prop in t) return t[prop];
    return stub;
  },
  has() {
    return true; // makes every identifier "defined" for the global scope
  },
  set(t, prop, value) {
    t[prop] = value;
    return true;
  },
});

vm.createContext(sandbox);

try {
  const script = new vm.Script(concat, { filename: "frontend-concat.js" });
  script.runInContext(sandbox);
} catch (err) {
  console.error("Frontend load smoke FAILED:");
  console.error(err && err.stack ? err.stack : err);
  // Try to point at the offending concat line for stack frames we control.
  const stackMatch = err && err.stack && err.stack.match(/frontend-concat\.js:(\d+)/);
  if (stackMatch) {
    const lineNo = parseInt(stackMatch[1], 10);
    const owner = lineOwners[lineNo - 1] || "?";
    const text = concat.split("\n")[lineNo - 1] || "";
    console.error(`  at concat line ${lineNo} (from ${owner}): ${text.trim()}`);
  }
  process.exit(1);
}

console.log(`OK: loaded ${srcs.length} scripts in order: ${srcs.join(", ")}`);
