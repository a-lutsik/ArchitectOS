/* ArchitectOS rich assistant responses: Markdown + Mermaid + action chips.
   ES module. The vendored mermaid (/vendor/mermaid.min.js) stays a classic
   script injected on demand below; it registers window.mermaid. */
import { t } from "./state.js";
import { decorateAskMedia } from "./media-lightbox.js";

const ArchitectOSRich = (function (global) {
  "use strict";

  // Vendored copy (mermaid@10.9.3): the app CSP is script-src 'self', and a
  // CDN script without SRI would be both blocked and a supply-chain risk.
  const MERMAID_SRC = "/vendor/mermaid.min.js";
  let mermaidLoading = null;
  let mermaidReady = false;
  const SHELL_CODE_LANGS = new Set(["", "bash", "sh", "shell", "zsh", "fish", "powershell", "pwsh", "cmd", "bat", "console", "terminal", "text", "plaintext"]);

  function isShellCodeLang(lang) {
    return SHELL_CODE_LANGS.has(String(lang || "").toLowerCase());
  }

  function codeActionIcons() {
    return {
      copy: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>',
      run: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2.5"></rect><path d="M7 9l3 3-3 3M12 16h5"></path></svg>',
    };
  }

  function wrapRichCode(code, lang) {
    const runnable = isShellCodeLang(lang);
    const icons = codeActionIcons();
    const copyLabel = t("terminal.copy");
    const runLabel = t("terminal.runInTerminal");
    const runBtn = runnable
      ? `<button type="button" class="rich-code-action" data-rich-code-run title="${escapeHtml(runLabel)}" aria-label="${escapeHtml(runLabel)}">${icons.run}</button>`
      : "";
    return `<div class="rich-code-wrap${runnable ? " is-runnable" : ""}"><pre class="rich-code"><code>${escapeHtml(code)}</code></pre><div class="rich-code-actions"><button type="button" class="rich-code-action" data-rich-code-copy title="${escapeHtml(copyLabel)}" aria-label="${escapeHtml(copyLabel)}">${icons.copy}</button>${runBtn}</div></div>`;
  }

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
    const startMatch = source.match(/\{\s*"(?:actions|links|questions|advice)"\s*:/);
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
      if (!/^\{\s*"(?:actions|links|questions|advice)"/.test(body)) continue;
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
    const openPlain = source.match(/```[a-z]*\s*\n?\s*\{\s*"(?:actions|links|questions|advice)"[\s\S]*$/i);
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
      return { actions: [], links: [], questions: [], advice: [] };
    }
    const actions = Array.isArray(structured.actions) ? structured.actions.filter((item) => item && typeof item === "object") : [];
    const links = Array.isArray(structured.links) ? structured.links.filter((item) => item && typeof item === "object") : [];
    const questions = normalizeQuestions(structured.questions);
    const advice = normalizeAdvice(structured.advice);
    return { ...structured, actions, links, questions, advice };
  }

  function normalizeAdvice(raw) {
    if (!Array.isArray(raw)) return [];
    const keep = [];
    const seen = new Set();
    for (const item of raw) {
      if (!item || typeof item !== "object") continue;
      const kind = String(item.kind || "").toLowerCase();
      if (kind !== "contradicts" && kind !== "limits" && kind !== "supports") continue;
      const id = String(item.id || "").trim();
      const text = String(item.text || item.reason || "").trim();
      if (!id || !text) continue;
      const key = `${kind}:${id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      keep.push({
        kind,
        id,
        source: String(item.source || "memory"),
        label: String(item.label || id).trim(),
        text,
        confidence: Number(item.confidence) || 0,
      });
    }
    keep.sort((a, b) => {
      const order = { contradicts: 0, limits: 1, supports: 2 };
      return (order[a.kind] ?? 9) - (order[b.kind] ?? 9);
    });
    return keep.slice(0, 4);
  }

  function renderAdviceCallouts(advice) {
    if (!advice || !advice.length) return "";
    const rows = advice.map((item) => {
      const kind = item.kind === "contradicts" ? "contradicts" : item.kind === "limits" ? "limits" : "supports";
      const title = kind === "contradicts"
        ? t("ask.advice.contradicts")
        : kind === "limits"
          ? t("ask.advice.limits")
          : t("ask.advice.supports");
      const actionType = /azure|boards/i.test(String(item.source || "")) || /^AB#/i.test(String(item.label || ""))
        ? "open_work_item"
        : "memory_get";
      let actionId = String(item.id || "");
      if (actionType === "open_work_item") {
        const fromLabel = String(item.label || "").match(/(\d{3,7})/);
        const fromId = actionId.match(/(\d{3,7})/);
        actionId = (fromLabel && fromLabel[1]) || (fromId && fromId[1]) || actionId;
      }
      return `<button type="button" class="rich-advice-item is-${kind}" data-rich-action="${escapeHtml(actionType)}" data-rich-id="${escapeHtml(actionId)}" title="${escapeHtml(item.label || "")}">
        <span class="rich-advice-kind">${escapeHtml(title)}</span>
        <span class="rich-advice-label">${escapeHtml(item.label || item.id)}</span>
        <span class="rich-advice-text">${escapeHtml(item.text)}</span>
      </button>`;
    }).join("");
    return `<div class="rich-advice" role="status">${rows}</div>`;
  }

  function normalizeQuestions(raw) {
    if (!Array.isArray(raw)) return [];
    return raw.map(normalizeQuestion).filter(Boolean);
  }

  function normalizeQuestion(item) {
    if (!item || typeof item !== "object") return null;
    const prompt = String(item.prompt || item.title || item.question || "").trim();
    const options = (Array.isArray(item.options) ? item.options : []).map((opt, index) => {
      if (typeof opt === "string") {
        const label = opt.trim();
        return label ? { id: `opt-${index}`, label } : null;
      }
      if (!opt || typeof opt !== "object") return null;
      const label = String(opt.label || opt.text || "").trim();
      if (!label) return null;
      return { id: String(opt.id || `opt-${index}`), label };
    }).filter(Boolean);
    if (!prompt || options.length < 2) return null;
    return { prompt, options, allowMultiple: Boolean(item.allow_multiple || item.allowMultiple) };
  }

  const BULLET_RE = /^\s*(?:[-*•]|\d+[.)]|[a-z][.)])\s+(.+?)\s*$/i;
  const ASK_RE = /please|point me|tell me|choose|pick|select|one of these|one of the following|which of|would you|should i|can you (?:point|tell|share)|уточн|выбер|укажите|что из|какой из|какой вариант/i;

  function isChoiceIntro(line) {
    const text = String(line || "").trim();
    if (text.length < 8 || text.length > 320) return false;
    if (ASK_RE.test(text) && /[?:：]\s*$/.test(text)) return true;
    if (/\?\s*$/.test(text) && /which|what|who|where|how|какой|что|где|как/i.test(text)) return true;
    return false;
  }

  function cleanOptionLabel(text) {
    return String(text || "").trim().replace(/[,;]?\s+or\s*$/i, "").trim();
  }

  function extractClarifyQuestion(source) {
    const text = String(source || "").replace(/\r\n/g, "\n");
    const lines = text.split("\n");
    let inFence = false;
    let best = null;
    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];
      if (/^\s*```/.test(line)) {
        inFence = !inFence;
        continue;
      }
      if (inFence || !isChoiceIntro(line)) continue;
      let cursor = index + 1;
      while (cursor < lines.length && !String(lines[cursor]).trim()) cursor += 1;
      const options = [];
      while (cursor < lines.length) {
        const match = lines[cursor].match(BULLET_RE);
        if (!match) break;
        const label = cleanOptionLabel(match[1]);
        if (!label || label.length > 180) {
          options.length = 0;
          break;
        }
        options.push(label);
        cursor += 1;
      }
      if (options.length >= 2 && options.length <= 8) {
        best = { start: index, end: cursor, prompt: line.trim(), options };
      }
    }
    if (!best) return { displayText: text.trim(), question: null };
    const displayText = [...lines.slice(0, best.start), ...lines.slice(best.end)].join("\n").trim();
    const prompt = best.prompt.replace(/[:：]\s*$/, "").trim();
    return {
      displayText,
      question: {
        prompt,
        options: best.options.map((label, index) => ({ id: `opt-${index}`, label })),
      },
    };
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
      const fence = line.match(/^\s*```\s*(\w+)?\s*$/);
      if (fence) {
        flushParagraph();
        const lang = (fence[1] || "").toLowerCase();
        const body = [];
        i += 1;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
          body.push(lines[i]);
          i += 1;
        }
        i += 1;
        const code = body.join("\n");
        if (lang === "mermaid") {
          parts.push(`<div class="rich-mermaid"><pre class="rich-mermaid-src" hidden>${escapeHtml(code)}</pre></div>`);
        } else {
          parts.push(wrapRichCode(code, lang));
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

  let questionSeq = 0;

  function renderQuestionCards(questions) {
    const items = Array.isArray(questions) ? questions.filter((item) => item && item.prompt && item.options?.length >= 2) : [];
    if (!items.length) return "";
    const otherLabel = t("ask.question.other");
    const continueLabel = t("ask.question.continue");
    const otherPlaceholder = t("ask.question.otherPlaceholder");
    return items.map((question) => {
      questionSeq += 1;
      const name = `ask-q-${questionSeq}`;
      const multiple = Boolean(question.allowMultiple);
      const type = multiple ? "checkbox" : "radio";
      const promptId = `${name}-prompt`;
      const options = question.options.map((option, index) => {
        const id = `${name}-${index}`;
        const value = escapeHtml(option.label);
        return `<label class="ask-question-option" for="${id}">
          <input id="${id}" type="${type}" name="${name}" value="${value}">
          <span class="ask-question-mark" aria-hidden="true"></span>
          <span class="ask-question-text">${inlineMarkdown(option.label)}</span>
        </label>`;
      }).join("");
      return `<form class="ask-question-card" data-ask-question="1" data-prompt="${escapeHtml(question.prompt)}" role="group" aria-labelledby="${promptId}">
        <p class="ask-question-prompt" id="${promptId}">${inlineMarkdown(question.prompt)}</p>
        <div class="ask-question-options" role="${multiple ? "group" : "radiogroup"}">${options}
          <label class="ask-question-option ask-question-option-other" for="${name}-other">
            <input id="${name}-other" type="${type}" name="${name}" value="__other__">
            <span class="ask-question-mark" aria-hidden="true"></span>
            <span class="ask-question-text">${escapeHtml(otherLabel)}</span>
          </label>
          <input class="ask-question-other-field" type="text" maxlength="500" placeholder="${escapeHtml(otherPlaceholder)}" hidden>
        </div>
        <div class="ask-question-toolbar">
          <button type="submit" class="ask-question-continue" disabled>${escapeHtml(continueLabel)}</button>
        </div>
      </form>`;
    }).join("");
  }

  const MERMAID_DIAGRAM_RE = /^\s*(flowchart|graph|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|gitGraph|mindmap|timeline|journey|C4Context|quadrantChart|sankey-beta|xychart-beta|block-beta)\b/i;

  function decodeMermaidEntities(value) {
    return String(value || "")
      .replace(/&quot;/g, '"')
      .replace(/&#39;|&apos;/g, "'")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&");
  }

  function encodeMermaidLabel(label) {
    return String(label || "")
      .replace(/#/g, "#35;")
      .replace(/"/g, "#quot;")
      .replace(/\(/g, "#40;")
      .replace(/\)/g, "#41;")
      .replace(/</g, "#60;")
      .replace(/>/g, "#62;")
      .replace(/\[/g, "#91;")
      .replace(/\]/g, "#93;");
  }

  function normalizeMermaidSource(raw) {
    let text = decodeMermaidEntities(raw).replace(/\r\n/g, "\n").replace(/\u00a0/g, " ").trim();
    text = text.replace(/^```(?:mermaid)?\s*/i, "").replace(/\s*```$/i, "").trim();
    text = text.replace(/[“”]/g, '"').replace(/[‘’]/g, "'");
    // Cursor/Ask models emit YAML + layout:elk; vendored mermaid@10.9 has no ELK.
    text = text.replace(/^---\s*\n[\s\S]*?\n---\s*/, "").trim();
    text = text.replace(/^(\s*subgraph\s+)([A-Za-z][\w-]*)\s*\[(["'])([\s\S]*?)\3\]/gm, (_, pre, id, _q, title) => (
      `${pre}${id} ["${encodeMermaidLabel(title)}"]`
    ));
    text = text.replace(/^(\s*subgraph\s+)([A-Za-z][\w-]*)\[/gm, "$1$2 [");
    text = text.replace(/(\b[A-Za-z][\w-]*)\[(["'])([\s\S]*?)\2\]/g, (_, id, _q, label) => (
      `${id}["${encodeMermaidLabel(label)}"]`
    ));
    text = text.replace(/(\b[A-Za-z][\w-]*)\[([^\]\n"]+)\]/g, (full, id, label) => {
      if (/[()<>\/,:]/.test(label)) return `${id}["${encodeMermaidLabel(label)}"]`;
      return full;
    });
    return text;
  }

  function mermaidScratchIds(id) {
    return [id, `d${id}`, `i${id}`];
  }

  function removeMermaidScratch(id) {
    for (const scratchId of mermaidScratchIds(id)) {
      document.getElementById(scratchId)?.remove();
    }
  }

  function sweepOrphanMermaidErrors() {
    document.querySelectorAll("svg").forEach((svg) => {
      if (svg.closest(".rich-mermaid.rendered")) return;
      const text = svg.textContent || "";
      if (!/Syntax error in text/i.test(text) || !/mermaid version/i.test(text)) return;
      const host = svg.parentElement;
      svg.remove();
      if (host && host !== document.body && host.id && /^d/.test(host.id) && !host.childElementCount) {
        host.remove();
      }
    });
  }

  function isMermaidErrorSvg(svg) {
    const text = typeof svg === "string" ? svg : "";
    return /Syntax error in text/i.test(text) && /mermaid version/i.test(text);
  }

  function mermaidFallback(node, definition, message) {
    node.classList.add("rendered");
    node.innerHTML = `${wrapRichCode(definition, "mermaid")}<p class="rich-mermaid-error">${escapeHtml(message || "Diagram could not be rendered.")}</p>`;
  }

  function uiIsDark() {
    return document.documentElement.getAttribute("data-theme") === "dark";
  }

  function parseRgb(value) {
    if (!value || value === "none" || value === "transparent") return null;
    const canvas = parseRgb.ctx || (parseRgb.ctx = document.createElement("canvas").getContext("2d"));
    canvas.fillStyle = "#000000";
    canvas.fillStyle = String(value).trim();
    const computed = canvas.fillStyle;
    const match = String(computed).match(/rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
    if (!match) return null;
    return [Number(match[1]), Number(match[2]), Number(match[3])];
  }

  function relativeLuminance(rgb) {
    const toLinear = (channel) => {
      const value = channel / 255;
      return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * toLinear(rgb[0]) + 0.7152 * toLinear(rgb[1]) + 0.0722 * toLinear(rgb[2]);
  }

  function contrastInk(rgb) {
    return relativeLuminance(rgb) > 0.42 ? "#09090b" : "#fafafa";
  }

  function elementPaint(el, prop) {
    if (!el) return null;
    const attr = el.getAttribute(prop);
    if (attr && attr !== "none") return parseRgb(attr);
    const style = el.getAttribute("style") || "";
    const match = style.match(new RegExp(`${prop}\\s*:\\s*([^;]+)`, "i"));
    if (match) return parseRgb(match[1]);
    return null;
  }

  function setLabelInk(root, ink) {
    root.querySelectorAll("text, tspan").forEach((el) => {
      el.setAttribute("fill", ink);
      el.style.fill = ink;
      el.style.color = ink;
    });
    root.querySelectorAll("span, div, p, a").forEach((el) => {
      el.style.color = ink;
    });
  }

  function recolorMermaidSvg(svgMarkup) {
    const wrap = document.createElement("div");
    wrap.innerHTML = svgMarkup;
    const svg = wrap.querySelector("svg");
    if (!svg) return svgMarkup;
    svg.setAttribute("role", "img");
    svg.querySelectorAll("g").forEach((group) => {
      const shape = group.querySelector(":scope > rect, :scope > polygon, :scope > circle, :scope > ellipse, :scope > path.node");
      if (!shape) return;
      const hasLabel = group.querySelector(":scope > .label, :scope > .nodeLabel, :scope > text, :scope > foreignObject, .cluster-label");
      if (!hasLabel && !group.classList.contains("node") && !group.classList.contains("cluster") && !group.classList.contains("block")) return;
      const fill = elementPaint(shape, "fill");
      if (!fill) return;
      setLabelInk(group, contrastInk(fill));
      if (group.classList.contains("cluster")) {
        const stroke = elementPaint(shape, "stroke");
        if (!stroke || Math.abs(relativeLuminance(fill) - relativeLuminance(stroke)) < 0.12) {
          shape.setAttribute("stroke", contrastInk(fill) === "#09090b" ? "#71717a" : "#a1a1aa");
        }
      }
    });
    const edgeInk = uiIsDark() ? "#c4c4cc" : "#3f3f46";
    svg.querySelectorAll(".edgePath path, .flowchart-link, path.flowchart-link, path[marker-end], .edge path").forEach((path) => {
      const stroke = elementPaint(path, "stroke");
      if (!stroke || relativeLuminance(stroke) > 0.62 || relativeLuminance(stroke) < 0.08) {
        path.setAttribute("stroke", edgeInk);
        path.style.stroke = edgeInk;
      }
      const width = Number(path.getAttribute("stroke-width") || 1);
      if (width < 1.5) path.setAttribute("stroke-width", "1.7");
    });
    svg.querySelectorAll("marker path, marker polygon").forEach((mark) => {
      mark.setAttribute("fill", edgeInk);
      mark.style.fill = edgeInk;
    });
    return wrap.innerHTML;
  }

  function mermaidThemeConfig() {
    const dark = uiIsDark();
    return {
      startOnLoad: false,
      theme: dark ? "dark" : "base",
      securityLevel: "loose",
      fontFamily: "ui-sans-serif, system-ui, sans-serif",
      flowchart: {
        htmlLabels: false,
        useMaxWidth: true,
        curve: "linear",
        padding: 16,
        nodeSpacing: 36,
        rankSpacing: 48,
        diagramPadding: 12,
      },
      themeVariables: {
        darkMode: dark,
        background: "transparent",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        primaryColor: dark ? "#312e81" : "#e0e7ff",
        primaryTextColor: dark ? "#f8fafc" : "#18181b",
        primaryBorderColor: dark ? "#a5b4fc" : "#6366f1",
        lineColor: dark ? "#c4c4cc" : "#52525b",
        secondaryColor: dark ? "#1e3a5f" : "#e0f2fe",
        secondaryTextColor: dark ? "#f8fafc" : "#18181b",
        secondaryBorderColor: dark ? "#7dd3fc" : "#0284c7",
        tertiaryColor: dark ? "#27272a" : "#f4f4f5",
        tertiaryTextColor: dark ? "#f8fafc" : "#18181b",
        clusterBkg: dark ? "#1c1c22" : "#f8fafc",
        clusterBorder: dark ? "#a1a1aa" : "#a1a1aa",
        titleColor: dark ? "#f8fafc" : "#18181b",
        nodeTextColor: dark ? "#f8fafc" : "#18181b",
        edgeLabelBackground: dark ? "#18181b" : "#ffffff",
      },
    };
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
          global.mermaid.initialize(mermaidThemeConfig());
          if (typeof global.mermaid.setParseErrorHandler === "function") {
            global.mermaid.setParseErrorHandler(() => {});
          } else {
            global.mermaid.parseError = () => {};
          }
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
    const nodes = [...(root.querySelectorAll(".rich-mermaid:not(.rendered)") || [])];
    if (!nodes.length) return;
    try {
      const mermaid = await ensureMermaid();
      mermaid.initialize(mermaidThemeConfig());
      if (typeof mermaid.setParseErrorHandler === "function") mermaid.setParseErrorHandler(() => {});
      else mermaid.parseError = () => {};
      for (let i = 0; i < nodes.length; i += 1) {
        const node = nodes[i];
        const src = node.querySelector(".rich-mermaid-src");
        const raw = src ? src.textContent : (node.getAttribute("data-mermaid") || "");
        const definition = normalizeMermaidSource(raw);
        if (!definition) continue;
        if (!MERMAID_DIAGRAM_RE.test(definition)) {
          mermaidFallback(node, raw || definition, "Diagram is missing a mermaid header (flowchart, sequenceDiagram, …).");
          continue;
        }
        const id = `aosMermaid${Date.now()}${i}`;
        try {
          const result = await mermaid.render(id, definition);
          removeMermaidScratch(id);
          if (!result?.svg || isMermaidErrorSvg(result.svg)) {
            mermaidFallback(node, raw || definition, "Diagram could not be rendered.");
            continue;
          }
          const srcKeep = `<pre class="rich-mermaid-src" hidden>${escapeHtml(raw)}</pre>`;
          node.innerHTML = srcKeep + recolorMermaidSvg(result.svg);
          node.classList.add("rendered");
          decorateAskMedia(node);
        } catch (_err) {
          removeMermaidScratch(id);
          mermaidFallback(node, raw || definition, "Diagram could not be rendered.");
        }
      }
    } catch (_err) {
      for (const node of nodes) {
        const src = node.querySelector(".rich-mermaid-src");
        const definition = normalizeMermaidSource(src ? src.textContent : (node.getAttribute("data-mermaid") || ""));
        mermaidFallback(node, definition, "Diagram could not be rendered.");
      }
    }
    sweepOrphanMermaidErrors();
  }

  function parseResponse(text, structuredHint) {
    const extracted = extractArchitectosBlock(text);
    const structured = normalizeStructured(structuredHint || extracted.structured);
    let displayText = extracted.displayText;
    const detected = extractClarifyQuestion(displayText);
    if (detected.question) displayText = detected.displayText;
    if (!structured.questions.length && detected.question) {
      structured.questions = [detected.question];
    }
    const links = autoLinksFromText(displayText, structured);
    return {
      displayText,
      structured: { ...structured, links },
    };
  }

  function renderInto(element, text, structuredHint) {
    if (!element) return null;
    const parsed = parseResponse(text, structuredHint);
    element.classList.add("rich");
    element.innerHTML = renderAdviceCallouts(parsed.structured.advice)
      + renderMarkdown(parsed.displayText)
      + renderQuestionCards(parsed.structured.questions)
      + renderActionChips(parsed.structured, parsed.structured.links);
    sweepOrphanMermaidErrors();
    void paintMermaid(element).finally(() => {
      decorateAskMedia(element);
      sweepOrphanMermaidErrors();
      setTimeout(sweepOrphanMermaidErrors, 50);
    });
    element.dispatchEvent(new Event("aos-rich-rendered", { bubbles: true }));
    return parsed;
  }

  function setPlain(element, text) {
    if (!element) return;
    element.classList.remove("rich");
    element.textContent = text || "";
  }

  return {
    extractArchitectosBlock,
    extractClarifyQuestion,
    parseResponse,
    renderInto,
    setPlain,
    renderMarkdown,
    normalizeMermaidSource,
  };
})(window);

export { ArchitectOSRich };
