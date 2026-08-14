/* ArchitectOS rich assistant responses: Markdown + Mermaid + action chips. */
(function (global) {
  "use strict";

  // Vendored copy (mermaid@10.9.3): the app CSP is script-src 'self', and a
  // CDN script without SRI would be both blocked and a supply-chain risk.
  const MERMAID_SRC = "/vendor/mermaid.min.js";
  let mermaidLoading = null;
  let mermaidReady = false;

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "'": "&#39;",
      '"': "&quot;",
    }[char]));
  }

  function extractBareStructuredJson(source) {
    // Models sometimes emit the structured payload without an ```architectos fence
    // (or the stream cuts the fence off). Grab {"actions": …} / {"links": …} via a
    // balanced-brace scan so raw JSON never leaks into the rendered answer.
    const startMatch = source.match(/\{\s*"(?:actions|links)"\s*:/);
    if (!startMatch) return null;
    const start = startMatch.index;
    let depth = 0;
    let inString = false;
    let escaped = false;
    for (let i = start; i < source.length; i += 1) {
      const ch = source[i];
      if (inString) {
        if (escaped) escaped = false;
        else if (ch === "\\") escaped = true;
        else if (ch === '"') inString = false;
        continue;
      }
      if (ch === '"') inString = true;
      else if (ch === "{") depth += 1;
      else if (ch === "}") {
        depth -= 1;
        if (depth === 0) {
          const raw = source.slice(start, i + 1);
          try {
            const structured = JSON.parse(raw);
            if (structured && typeof structured === "object") {
              return { start, end: i + 1, structured, raw };
            }
          } catch (_err) {
            // Looks like the structured payload but does not parse (truncated
            // label, smart quotes, …) — still hide it rather than show raw JSON.
            return { start, end: i + 1, structured: null, raw };
          }
          return null;
        }
      }
    }
    // Truncated mid-JSON: hide the tail so partial JSON is never shown.
    return { start, end: source.length, structured: null, raw: source.slice(start) };
  }

  function extractArchitectosBlock(text) {
    const source = String(text || "");
    const match = source.match(/```architectos\s*([\s\S]*?)```/i);
    if (match) {
      let structured = null;
      try {
        structured = JSON.parse(match[1].trim());
      } catch (_err) {
        structured = null;
      }
      const displayText = (source.slice(0, match.index) + source.slice(match.index + match[0].length)).trim();
      return { displayText, structured, rawBlock: match[0] };
    }
    // Unclosed fence (stream still going or cut off): hide the partial block.
    const open = source.match(/```architectos\s*[\s\S]*$/i);
    if (open) {
      return { displayText: source.slice(0, open.index).trim(), structured: null, rawBlock: open[0] };
    }
    // Plain fenced block (``` or ```json) whose payload is the structured
    // actions/links JSON — models often drop the `architectos` language tag.
    const fenceRe = /```[a-z]*\s*\n?([\s\S]*?)```/gi;
    let fence;
    while ((fence = fenceRe.exec(source)) !== null) {
      const body = fence[1].trim();
      if (!/^\{\s*"(?:actions|links)"/.test(body)) continue;
      try {
        const structured = JSON.parse(body);
        const displayText = (source.slice(0, fence.index) + source.slice(fence.index + fence[0].length)).trim();
        return { displayText, structured, rawBlock: fence[0] };
      } catch (_err) {
        // Payload looks like the structured JSON but does not parse — hide it
        // anyway; raw model JSON should never reach the rendered answer.
        const displayText = (source.slice(0, fence.index) + source.slice(fence.index + fence[0].length)).trim();
        return { displayText, structured: null, rawBlock: fence[0] };
      }
    }
    // Unclosed plain fence whose payload starts like the structured JSON.
    const openPlain = source.match(/```[a-z]*\s*\n?\s*\{\s*"(?:actions|links)"[\s\S]*$/i);
    if (openPlain) {
      return { displayText: source.slice(0, openPlain.index).trim(), structured: null, rawBlock: openPlain[0] };
    }
    const bare = extractBareStructuredJson(source);
    if (bare) {
      const displayText = (source.slice(0, bare.start) + source.slice(bare.end)).trim();
      return { displayText, structured: bare.structured, rawBlock: bare.raw };
    }
    return { displayText: source.trim(), structured: null, rawBlock: "" };
  }

  function normalizeStructured(structured) {
    if (!structured || typeof structured !== "object") {
      return { actions: [], links: [] };
    }
    const actions = Array.isArray(structured.actions) ? structured.actions.filter((item) => item && typeof item === "object") : [];
    const links = Array.isArray(structured.links) ? structured.links.filter((item) => item && typeof item === "object") : [];
    return { actions, links, ...structured };
  }

  function autoLinksFromText(text, structured) {
    const links = [...(structured.links || [])];
    const seen = new Set(links.map((item) => `${item.kind || ""}:${item.id || ""}`));
    const workIds = String(text || "").match(/\b(?:AB#|ado[:\s#]?|#)?(\d{3,7})\b/gi) || [];
    for (const raw of workIds) {
      const id = String(raw).replace(/\D/g, "");
      if (!id) continue;
      const key = `work_item:${id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      links.push({ kind: "work_item", id, title: `AB#${id}` });
    }
    const memoryIds = String(text || "").match(/\bid=([a-zA-Z0-9_-]{6,})\b/g) || [];
    for (const raw of memoryIds) {
      const id = raw.replace(/^id=/, "");
      const key = `memory:${id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      links.push({ kind: "memory", id, title: id });
    }
    return links;
  }

  function inlineMarkdown(text) {
    let html = escapeHtml(text);
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/(^|[^\*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    html = html.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label, href) => {
      const safeHref = String(href || "");
      if (/^(https?:|ab:|ado:|memory:|mailto:|#)/i.test(safeHref)) {
        return `<a href="${escapeHtml(safeHref)}" data-rich-link="${escapeHtml(safeHref)}">${label}</a>`;
      }
      return label;
    });
    html = html.replace(/\bAB#(\d{3,7})\b/g, '<a href="ab://$1" data-rich-link="ab://$1">AB#$1</a>');
    return html;
  }

  function isTableSeparator(line) {
    return /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(line);
  }

  function parseTable(lines, start) {
    const header = lines[start];
    if (!header.includes("|") || start + 1 >= lines.length || !isTableSeparator(lines[start + 1])) {
      return null;
    }
    const rows = [];
    let index = start;
    while (index < lines.length && lines[index].includes("|")) {
      if (index === start + 1 && isTableSeparator(lines[index])) {
        index += 1;
        continue;
      }
      const cells = lines[index].replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
      rows.push(cells);
      index += 1;
      if (index < lines.length && !lines[index].includes("|")) break;
    }
    if (rows.length < 1) return null;
    const head = rows[0];
    const body = rows.slice(1);
    let html = "<table class=\"rich-table\"><thead><tr>";
    html += head.map((cell) => `<th>${inlineMarkdown(cell)}</th>`).join("");
    html += "</tr></thead><tbody>";
    for (const row of body) {
      html += "<tr>" + row.map((cell) => `<td>${inlineMarkdown(cell)}</td>`).join("") + "</tr>";
    }
    html += "</tbody></table>";
    return { html, next: index };
  }

  function renderMarkdown(text) {
    const source = String(text || "").replace(/\r\n/g, "\n");
    if (!source.trim()) return "";
    // Whitelisted HTML: models emit <details>/<summary> for tool traces. Extract
    // the blocks before escaping, render their bodies as markdown, re-insert after.
    const detailsBlocks = [];
    const preprocessed = source.replace(/<details[^>]*>\s*([\s\S]*?)\s*<\/details>/gi, (_m, inner) => {
      detailsBlocks.push(inner);
      return `\n@@AOS_DETAILS_${detailsBlocks.length - 1}@@\n`;
    });
    const lines = preprocessed.split("\n");
    const parts = [];
    let i = 0;
    let paragraph = [];

    function flushParagraph() {
      if (!paragraph.length) return;
      const joined = paragraph.join("\n").trim();
      if (joined) parts.push(`<p>${inlineMarkdown(joined).replace(/\n/g, "<br>")}</p>`);
      paragraph = [];
    }

    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^```(\w+)?\s*$/);
      if (fence) {
        flushParagraph();
        const lang = (fence[1] || "").toLowerCase();
        const body = [];
        i += 1;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        i += 1;
        const code = body.join("\n");
        if (lang === "mermaid") {
          parts.push(`<div class="rich-mermaid" data-mermaid="${escapeHtml(code)}"></div>`);
        } else {
          parts.push(`<pre class="rich-code"><code>${escapeHtml(code)}</code></pre>`);
        }
        continue;
      }

      if (/^\s*\|/.test(line) && line.includes("|")) {
        const table = parseTable(lines, i);
        if (table) {
          flushParagraph();
          parts.push(table.html);
          i = table.next;
          continue;
        }
      }

      if (/^\s*$/.test(line)) {
        flushParagraph();
        i += 1;
        continue;
      }

      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        flushParagraph();
        const level = heading[1].length;
        parts.push(`<h${level} class="rich-h">${inlineMarkdown(heading[2])}</h${level}>`);
        i += 1;
        continue;
      }

      const ul = line.match(/^\s*[-*]\s+(.+)$/);
      if (ul) {
        flushParagraph();
        const items = [];
        while (i < lines.length) {
          const item = lines[i].match(/^\s*[-*]\s+(.+)$/);
          if (!item) break;
          items.push(`<li>${inlineMarkdown(item[1])}</li>`);
          i += 1;
        }
        parts.push(`<ul class="rich-list">${items.join("")}</ul>`);
        continue;
      }

      const ol = line.match(/^\s*\d+\.\s+(.+)$/);
      if (ol) {
        flushParagraph();
        const items = [];
        while (i < lines.length) {
          const item = lines[i].match(/^\s*\d+\.\s+(.+)$/);
          if (!item) break;
          items.push(`<li>${inlineMarkdown(item[1])}</li>`);
          i += 1;
        }
        parts.push(`<ol class="rich-list">${items.join("")}</ol>`);
        continue;
      }

      paragraph.push(line);
      i += 1;
    }
    flushParagraph();
    let html = parts.join("");
    html = html.replace(/<p>@@AOS_DETAILS_(\d+)@@<\/p>|@@AOS_DETAILS_(\d+)@@/g, (_m, inParagraph, bare) => {
      const index = Number(inParagraph ?? bare);
      const inner = String(detailsBlocks[index] || "");
      const summaryMatch = inner.match(/<summary[^>]*>([\s\S]*?)<\/summary>/i);
      const summary = summaryMatch ? summaryMatch[1].trim() : "Details";
      const body = summaryMatch ? inner.slice(summaryMatch.index + summaryMatch[0].length).trim() : inner.trim();
      return `<details class="rich-details"><summary>${inlineMarkdown(summary)}</summary><div class="rich-details-body">${renderMarkdown(body)}</div></details>`;
    });
    return html;
  }

  function actionLabel(action) {
    if (action.label) return String(action.label);
    const type = String(action.type || "");
    if (type === "open_work_item" || type === "boards_get_item") return `Open AB#${action.id || ""}`.trim();
    if (type === "memory_get") return `Open memory ${action.id || ""}`.trim();
    if (type === "ask") return String(action.prompt || "Ask follow-up").slice(0, 48);
    if (type === "boards_search") return `Search boards: ${action.query || ""}`.slice(0, 48);
    return type || "Action";
  }

  function renderActionChips(structured, links) {
    const actions = [...(structured.actions || [])];
    for (const link of links || []) {
      const kind = String(link.kind || "");
      const id = String(link.id || "");
      if (!id) continue;
      if (kind === "work_item" && !actions.some((a) => String(a.id) === id && /work_item|boards_get/.test(String(a.type || "")))) {
        actions.push({ type: "open_work_item", id, label: link.title || `Open AB#${id}` });
      }
      if (kind === "memory" && !actions.some((a) => String(a.id) === id && String(a.type) === "memory_get")) {
        actions.push({ type: "memory_get", id, label: link.title || `Open ${id}` });
      }
    }
    if (!actions.length) return "";
    return `<div class="rich-actions">${actions.map((action, index) => {
      const type = escapeHtml(action.type || "");
      const id = escapeHtml(action.id || "");
      const prompt = escapeHtml(action.prompt || "");
      const query = escapeHtml(action.query || "");
      return `<button type="button" class="rich-action-chip" data-rich-action="${type}" data-rich-id="${id}" data-rich-prompt="${prompt}" data-rich-query="${query}" data-rich-index="${index}">${escapeHtml(actionLabel(action))}</button>`;
    }).join("")}</div>`;
  }

  async function ensureMermaid() {
    if (mermaidReady && global.mermaid) return global.mermaid;
    if (mermaidLoading) return mermaidLoading;
    mermaidLoading = new Promise((resolve, reject) => {
      if (global.mermaid) {
        mermaidReady = true;
        resolve(global.mermaid);
        return;
      }
      const script = document.createElement("script");
      script.src = MERMAID_SRC;
      script.async = true;
      script.onload = () => {
        try {
          global.mermaid.initialize({
            startOnLoad: false,
            theme: "dark",
            securityLevel: "strict",
            fontFamily: "inherit",
          });
          mermaidReady = true;
          resolve(global.mermaid);
        } catch (err) {
          reject(err);
        }
      };
      script.onerror = () => reject(new Error("Failed to load Mermaid"));
      document.head.appendChild(script);
    });
    return mermaidLoading;
  }

  async function paintMermaid(root) {
    const nodes = [...(root.querySelectorAll(".rich-mermaid[data-mermaid]") || [])];
    if (!nodes.length) return;
    try {
      const mermaid = await ensureMermaid();
      for (let i = 0; i < nodes.length; i += 1) {
        const node = nodes[i];
        const definition = node.getAttribute("data-mermaid") || "";
        if (!definition.trim()) continue;
        const id = `aos-mermaid-${Date.now()}-${i}`;
        try {
          const result = await mermaid.render(id, definition.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/&quot;/g, '"'));
          node.innerHTML = result.svg || "";
          node.removeAttribute("data-mermaid");
          node.classList.add("rendered");
        } catch (err) {
          node.innerHTML = `<pre class="rich-code"><code>${escapeHtml(definition)}</code></pre><p class="rich-mermaid-error">Diagram could not be rendered.</p>`;
        }
      }
    } catch (_err) {
      for (const node of nodes) {
        const definition = node.getAttribute("data-mermaid") || "";
        node.innerHTML = `<pre class="rich-code"><code>${escapeHtml(definition)}</code></pre>`;
      }
    }
  }

  function parseResponse(text, structuredHint) {
    const extracted = extractArchitectosBlock(text);
    const structured = normalizeStructured(structuredHint || extracted.structured);
    const links = autoLinksFromText(extracted.displayText, structured);
    return {
      displayText: extracted.displayText,
      structured: { ...structured, links },
    };
  }

  function renderInto(element, text, structuredHint) {
    if (!element) return null;
    const parsed = parseResponse(text, structuredHint);
    element.classList.add("rich");
    element.innerHTML = renderMarkdown(parsed.displayText) + renderActionChips(parsed.structured, parsed.structured.links);
    paintMermaid(element);
    return parsed;
  }

  function setPlain(element, text) {
    if (!element) return;
    element.classList.remove("rich");
    element.textContent = text || "";
  }

  global.ArchitectOSRich = {
    extractArchitectosBlock,
    parseResponse,
    renderInto,
    setPlain,
    renderMarkdown,
  };
})(window);
