#!/usr/bin/env node
// No-browser smoke test for the ES-module frontend.
//
// The frontend is native ESM: index.html loads exactly one module entry
// (frontend/main.js) which imports every other module. This script validates
// the new contract, dependency-free:
//
//   1. index.html loads /main.js via <script type="module"> and has no
//      leftover classic <script src> tags for app files.
//   2. Static import graph: every relative import resolves to an existing
//      file, and every named import matches a named export of the target.
//   3. Cycle detection: strongly connected components of the import graph
//      must match KNOWN_CYCLES below (call-time-only cycles verified during
//      the module conversion). Any new cycle fails the build.
//   4. Runtime: the real module graph is imported by Node with browser
//      globals stubbed on globalThis — catches link errors, TDZ mistakes
//      and top-level throws. (Async bootstrap errors are swallowed by the
//      app's own .catch(showError), same as the old classic-script smoke.)
//
// Run: node scripts/frontend_load_smoke.js   (exit 0 = OK)

const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");

const ROOT = path.resolve(__dirname, "..");
const FRONTEND = path.join(ROOT, "frontend");
const ENTRY = "main.js";

let failures = 0;
function fail(message) {
  failures += 1;
  console.error(`FAIL: ${message}`);
}

// ---------------------------------------------------------------------------
// 1. index.html contract
// ---------------------------------------------------------------------------
const html = fs.readFileSync(path.join(FRONTEND, "index.html"), "utf8");

const moduleScripts = [...html.matchAll(/<script\s+[^>]*type="module"[^>]*src="\/([^"]+)"[^>]*>\s*<\/script>/g)].map(m => m[1]);
const classicScripts = [...html.matchAll(/<script\s+src="\/([^"]+\.js)"\s*>\s*<\/script>/g)].map(m => m[1]);
const allScriptTags = [...html.matchAll(/<script\b[^>]*>/g)].map(m => m[0]);

if (moduleScripts.length !== 1 || moduleScripts[0] !== ENTRY) {
  fail(`index.html must load exactly one module entry "/${ENTRY}"; found module scripts: ${moduleScripts.join(", ") || "(none)"}`);
}
if (classicScripts.length > 0) {
  fail(`index.html has leftover classic <script src> tags: ${classicScripts.join(", ")}`);
}
const unexpected = allScriptTags.filter(tag => !/type="module"/.test(tag));
if (unexpected.length > 0) {
  fail(`index.html has non-module <script> tags: ${unexpected.join(" | ")}`);
}

// ---------------------------------------------------------------------------
// 2. Static import graph: resolution + named-export existence
// ---------------------------------------------------------------------------
const moduleFiles = fs.readdirSync(FRONTEND).filter(f => f.endsWith(".js")).sort();

// Supported shapes (the codebase uses only these; anything else fails loudly
// so the checker never silently under-validates):
//   import "./x.js";
//   import { a, b as c } from "./x.js";
//   export { a, b };
//   export { a as b };            (re-export forms are not used)
//   export function/const/...     (not used, but parsed for completeness)
const IMPORT_SIDE_EFFECT = /^\s*import\s+"(\.\/[^"]+)"\s*;/gm;
const IMPORT_NAMED = /^\s*import\s*\{([^}]*)\}\s*from\s*"(\.\/[^"]+)"\s*;/gms;
const EXPORT_LIST = /^\s*export\s*\{([^}]*)\}\s*;/gms;
const EXPORT_DECL = /^\s*export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)/gm;
const ANY_IMPORT = /^\s*import\s/gm;
const ANY_EXPORT = /^\s*export\s/gm;

const graph = new Map(); // file -> Set(files)
const exportsOf = new Map(); // file -> Set(names)
const pendingChecks = []; // {file, target, name} named-import checks

for (const file of moduleFiles) {
  const src = fs.readFileSync(path.join(FRONTEND, file), "utf8");
  const deps = new Set();
  const exp = new Set();

  // Exports
  for (const m of src.matchAll(EXPORT_LIST)) {
    for (const part of m[1].split(",")) {
      const name = part.trim();
      if (!name) continue;
      const as = name.match(/^(?:[A-Za-z_$][\w$]*\s+as\s+)?([A-Za-z_$][\w$]*)$/);
      if (!as) { fail(`${file}: unparseable export entry "${name}"`); continue; }
      exp.add(as[1]);
    }
  }
  for (const m of src.matchAll(EXPORT_DECL)) exp.add(m[1]);

  // Imports (named form, single- or multi-line)
  for (const m of src.matchAll(IMPORT_NAMED)) {
    const spec = m[2];
    deps.add(spec);
    const target = spec.replace(/^\.\//, "");
    if (!moduleFiles.includes(target)) {
      fail(`${file}: import "${spec}" does not resolve to a frontend module`);
      continue;
    }
    for (const part of m[1].split(",")) {
      const entry = part.trim();
      if (!entry) continue;
      const nm = entry.match(/^([A-Za-z_$][\w$]*)(?:\s+as\s+[A-Za-z_$][\w$]*)?$/);
      if (!nm) { fail(`${file}: unparseable import entry "${entry}"`); continue; }
      // Named-export existence is checked below once all exports are known.
      pendingChecks.push({ file, target, name: nm[1] });
    }
  }
  for (const m of src.matchAll(IMPORT_SIDE_EFFECT)) {
    const spec = m[1];
    deps.add(spec);
    if (!moduleFiles.includes(spec.replace(/^\.\//, ""))) {
      fail(`${file}: import "${spec}" does not resolve to a frontend module`);
    }
  }

  // Loud failure on import/export forms this checker does not understand.
  const stripped = src
    .replace(IMPORT_NAMED, "")
    .replace(IMPORT_SIDE_EFFECT, "")
    .replace(EXPORT_LIST, "")
    .replace(EXPORT_DECL, "");
  const leftoverImports = stripped.match(ANY_IMPORT);
  const leftoverExports = stripped.match(ANY_EXPORT);
  if (leftoverImports) fail(`${file}: unsupported import form (${leftoverImports.length} remaining) — extend the smoke parser`);
  if (leftoverExports) fail(`${file}: unsupported export form (${leftoverExports.length} remaining) — extend the smoke parser`);

  graph.set(file, deps);
  exportsOf.set(file, exp);
}

// Deferred named-export existence checks.
for (const check of pendingChecks) {
  if (!exportsOf.get(check.target).has(check.name)) {
    fail(`${check.file}: imports "${check.name}" from ./${check.target}, but that module does not export it`);
  }
}

// ---------------------------------------------------------------------------
// 3. Cycle detection (Tarjan SCC). Cycles among ES modules are legal and safe
//    here because every cyclic edge is a call-time function reference — no
//    module reads a cyclic partner's bindings during evaluation (verified in
//    the conversion; the only top-level cross-module reads are app.js's
//    bootstrap, which is an acyclic sink, and projects.js's
//    fileFind.bindEvents(), where file-find.js is guaranteed to evaluate
//    first and has no top-level reads of its own). A NEW cycle means someone
//    added an import edge this guarantee was not checked for — fail loudly.
// ---------------------------------------------------------------------------
const KNOWN_CYCLES = [
  // One large strongly-connected component: the UI modules call each other
  // freely (switchView, refreshWorkspace, render helpers...). Verified safe
  // during the ESM conversion: every cyclic edge is a function/const read at
  // CALL time, all const initializers in these modules are pure data, and the
  // only top-level cross-module reads are app.js's bootstrap (app.js is an
  // acyclic sink, not in this SCC) and projects.js's fileFind.bindEvents()
  // (file-find.js always evaluates first — its own body has no top-level
  // reads). Adding a new cycle means re-checking those invariants.
  [
    "agent-activity.js", "ask-ui.js", "chat.js", "code-intel.js",
    "file-editor.js", "file-find.js", "graph.js", "memory-ingest.js",
    "memory-panel.js", "project-wizard.js", "projects.js", "providers.js",
    "search-palette.js", "settings.js", "terminal.js", "ui.js",
    "workspace-chat.js",
  ],
];

function stronglyConnectedComponents(g) {
  const index = new Map();
  const low = new Map();
  const onStack = new Set();
  const stack = [];
  let counter = 0;
  const sccs = [];
  function visit(node) {
    index.set(node, counter);
    low.set(node, counter);
    counter += 1;
    stack.push(node);
    onStack.add(node);
    for (const dep of g.get(node) || []) {
      const depFile = dep.replace(/^\.\//, "");
      if (!g.has(depFile)) continue;
      if (!index.has(depFile)) {
        visit(depFile);
        low.set(node, Math.min(low.get(node), low.get(depFile)));
      } else if (onStack.has(depFile)) {
        low.set(node, Math.min(low.get(node), index.get(depFile)));
      }
    }
    if (low.get(node) === index.get(node)) {
      const scc = [];
      let n;
      do {
        n = stack.pop();
        onStack.delete(n);
        scc.push(n);
      } while (n !== node);
      if (scc.length > 1 || (g.get(node) || new Set()).has("./" + node)) {
        sccs.push(scc.sort());
      }
    }
  }
  for (const node of g.keys()) if (!index.has(node)) visit(node);
  return sccs;
}

const actualCycles = stronglyConnectedComponents(graph).map(scc => scc.join("+")).sort();
const knownCycles = KNOWN_CYCLES.map(scc => [...scc].sort().join("+")).sort();
if (JSON.stringify(actualCycles) !== JSON.stringify(knownCycles)) {
  fail(`import cycle set changed.\n  expected: ${JSON.stringify(knownCycles)}\n  actual:   ${JSON.stringify(actualCycles)}\n  Cycles are only allowed when every cyclic edge is a call-time reference; update KNOWN_CYCLES in this script after verifying that.`);
}

// ---------------------------------------------------------------------------
// 4. Runtime: import the real graph with browser globals stubbed.
// ---------------------------------------------------------------------------
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

function setGlobal(name, value) {
  try {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  } catch (_err) {
    try { globalThis[name] = value; } catch (_err2) { /* leave as-is */ }
  }
}

const windowShim = globalThis;
setGlobal("document", documentStub);
setGlobal("localStorage", storageStub);
setGlobal("sessionStorage", storageStub);
setGlobal("setTimeout", () => 0);
setGlobal("clearTimeout", () => {});
setGlobal("setInterval", () => 0);
setGlobal("clearInterval", () => {});
setGlobal("requestAnimationFrame", () => 0);
setGlobal("cancelAnimationFrame", () => {});
setGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }));
setGlobal("fetch", () => Promise.resolve({ ok: true, json: () => Promise.resolve({}), text: () => Promise.resolve("") }));
setGlobal("location", { href: "http://localhost/", origin: "http://localhost", pathname: "/", search: "", hash: "", reload() {}, assign() {}, replace() {} });
setGlobal("history", { pushState() {}, replaceState() {}, back() {}, forward() {} });
setGlobal("alert", () => {});
setGlobal("confirm", () => false);
setGlobal("prompt", () => null);
setGlobal("FormData", class FormData { append() {} get() { return null; } getAll() { return []; } has() { return false; } });
setGlobal("CustomEvent", class CustomEvent { constructor(type, init) { this.type = type; Object.assign(this, init); } });
setGlobal("Event", class Event { constructor(type) { this.type = type; } });
setGlobal("EventSource", class EventSource { constructor() {} addEventListener() {} close() {} });
setGlobal("WebSocket", class WebSocket { constructor() {} send() {} close() {} addEventListener() {} });
setGlobal("MutationObserver", class MutationObserver { observe() {} disconnect() {} takeRecords() { return []; } });
setGlobal("ResizeObserver", class ResizeObserver { observe() {} unobserve() {} disconnect() {} });
setGlobal("IntersectionObserver", class IntersectionObserver { observe() {} unobserve() {} disconnect() {} });
setGlobal("getComputedStyle", () => stub);
setGlobal("navigator", { language: "en", languages: ["en"], userAgent: "node", clipboard: { writeText: () => Promise.resolve() } });
setGlobal("window", windowShim);
setGlobal("self", windowShim);
setGlobal("addEventListener", () => {});
setGlobal("removeEventListener", () => {});
setGlobal("dispatchEvent", () => true);

let unhandled = null;
process.on("unhandledRejection", reason => {
  unhandled = reason;
});

async function main() {
  if (failures > 0) {
    // Static contract already broken; still report, but skip execution.
    console.error(`\nFrontend module smoke FAILED (${failures} problem(s) before execution).`);
    process.exit(1);
  }
  try {
    await import(pathToFileURL(path.join(FRONTEND, ENTRY)).href);
    // Let queued microtasks (bootstrap's awaited stub fetches) settle.
    for (let i = 0; i < 20; i += 1) {
      await new Promise(resolve => setImmediate(resolve));
    }
  } catch (err) {
    fail(`module graph execution threw:\n${err && err.stack ? err.stack : err}`);
  }
  if (unhandled) {
    fail(`unhandled promise rejection during module execution:\n${unhandled && unhandled.stack ? unhandled.stack : unhandled}`);
  }
  if (failures > 0) {
    console.error(`\nFrontend module smoke FAILED (${failures} problem(s)).`);
    process.exit(1);
  }
  console.log(`OK: index.html loads /${ENTRY} as a module; ${moduleFiles.length} modules, import graph resolves, exports match, cycles: ${actualCycles.length} known group(s); module graph executed with browser stubs.`);
}

main();
