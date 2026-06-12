// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let items = [];
let currentFilter = "active";
let expandedId = null;
let adding = false;
let sessionId = null;
let currentModel = "";
let allSessions = [];
let isStreaming = false;
let abortController = null;

// Search state
let searchQuery = "";
let searchDebounce = null;

// Collapse state (persisted in localStorage)
let sidebarCollapsed = localStorage.getItem("reggia.sidebarCollapsed") === "1";
let reggiaCollapsed = localStorage.getItem("reggia.reggiaCollapsed") === "1";

// User settings (from GET /settings)
let userSettings = null;

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

const chatMessages = document.getElementById("chat-messages");
const chatInput = document.getElementById("chat-input");
const chatTitle = document.getElementById("chat-title-text");
const btnStopChat = document.getElementById("btn-stop-chat");
const btnSendChat = document.getElementById("btn-send-chat");

function autosizeInput() {
  chatInput.style.height = "auto";
  const newHeight = Math.min(chatInput.scrollHeight, 200);
  chatInput.style.height = newHeight + "px";
}

function updateSendButtonState() {
  const hasText = chatInput.value.trim().length > 0;
  btnSendChat.disabled = !hasText || isStreaming;
}

chatInput.addEventListener("input", () => {
  autosizeInput();
  updateSendButtonState();
});

chatInput.addEventListener("keydown", (e) => {
  // Enter (without shift, and not during IME composition) sends.
  // Shift+Enter inserts newline (default textarea behavior).
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    const val = chatInput.value.trim();
    if (val && !isStreaming) {
      sendMessage(val);
    }
  }
});

btnSendChat.addEventListener("click", () => {
  const val = chatInput.value.trim();
  if (val && !isStreaming) {
    sendMessage(val);
  }
});

btnStopChat.addEventListener("click", () => {
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
});

async function sendMessage(prompt) {
  chatInput.value = "";
  autosizeInput();
  isStreaming = true;
  btnSendChat.style.display = "none";
  btnStopChat.style.display = "";
  updateSendButtonState();
  removeEmptyState();

  appendMessage("user", prompt);

  const assistantDiv = appendMessage("assistant", "");
  assistantDiv.innerHTML = thinkingHtml();
  const state = { textBuf: "", readPages: [], denials: null, hasContent: false, receivedDelta: false };

  if (!sessionId) {
    await createNewSession();
  }
  const sessionAtSend = sessionId;
  const isFirstTurn = chatMessages.querySelectorAll(".msg-user").length === 1;

  abortController = new AbortController();

  try {
    const resp = await fetch(`/sessions/${sessionAtSend}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, model: currentModel }),
      signal: abortController.signal,
    });
    console.log("[SSE] fetch response status:", resp.status, "ok:", resp.ok);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let chunkCount = 0;

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        console.log("[SSE] stream done, total chunks:", chunkCount, "buffer leftover:", buffer.length);
        break;
      }

      chunkCount++;
      const decoded = decoder.decode(value, { stream: true });
      console.log("[SSE] chunk #" + chunkCount, "size:", decoded.length, "bytes:", value.length);
      buffer += decoded;
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const data = line.slice(6);
        if (!data.trim()) continue;

        try {
          const msg = JSON.parse(data);
          console.log("[SSE] parsed type:", msg.type);
          handleStreamMessage(msg, assistantDiv, state);
        } catch (e) {
          console.log("[SSE] parse error:", e.message, "data:", data.substring(0, 100));
        }
      }
    }
  } catch (err) {
    console.log("[SSE] catch error:", err.name, err.message);
    if (err.name === "AbortError") {
      if (!state.hasContent) {
        assistantDiv.innerHTML = "";
      }
    } else {
      assistantDiv.innerHTML = `<div class="msg-error">Connection error: ${err.message}</div>`;
    }
  }

  // Final fallback: if nothing came back, show a soft message
  if (!state.hasContent && !state.denials && !abortController?.signal.aborted) {
    console.log("[SSE] final fallback: showing error");
    assistantDiv.innerHTML = `<div class="msg-error">No response received.</div>`;
  }

  abortController = null;
  isStreaming = false;
  btnStopChat.style.display = "none";
  btnSendChat.style.display = "";
  updateSendButtonState();
  chatInput.focus();

  if (state.readPages.length > 0) {
    loadItems(currentFilter);
  }

  // Refresh sidebar so first-turn title generation appears
  if (isFirstTurn) {
    setTimeout(loadSessions, 800);
    setTimeout(loadSessions, 2500);
  } else {
    loadSessions();
  }
}

function thinkingHtml() {
  return `<div class="msg-thinking"><span class="dot"></span><span class="dot"></span><span class="dot"></span><span>thinking…</span></div>`;
}

// state = { textBuf, readPages, denials, hasContent, receivedDelta } — mutated in place
function handleStreamMessage(msg, container, state) {
  const dbg = (...args) => console.log("[SSE]", ...args);
  dbg(`type=${msg.type}`, msg.type === "assistant" ? `content_types=${(msg.message?.content||[]).map(b=>b.type).join(",")}` : "");

  let textChanged = false;

  switch (msg.type) {
    case "content_block_start":
      // A new content block is starting — reset delta tracking so
      // multi-turn agent loops don't skip later text blocks.
      state.receivedDelta = false;
      break;

    case "assistant":
      // Tool-use detection must run regardless of streaming state — the
      // read-indicator depends on it. Only the *text* is conditional: skip it
      // when we already streamed this turn's tokens via stream_event deltas,
      // otherwise the trailing complete message would double the text.
      if (msg.message?.content) {
        for (const block of msg.message.content) {
          if (block.type === "text" && !state.receivedDelta) {
            const added = block.text.length;
            state.textBuf += block.text;
            dbg(`text +${added}chars, total=${state.textBuf.length}`);
            textChanged = true;
          }
          if (block.type === "tool_use") {
            dbg(`tool_use: ${block.name}`);
            if (block.name === "Read") {
              const fp = block.input?.file_path || "";
              if (fp.includes("reggia") && !state.readPages.includes(fp)) {
                state.readPages.push(fp);
              }
            }
          }
        }
      }
      // msg.delta.text is also possible on assistant messages that arrive
      // mid-stream; treat them the same as content_block_delta.
      if (msg.delta?.text) {
        state.textBuf += msg.delta.text;
        state.receivedDelta = true;
        dbg(`delta +${msg.delta.text.length}chars, total=${state.textBuf.length}`);
        textChanged = true;
      }
      break;

    case "stream_event": {
      // With --include-partial-messages the CLI wraps raw streaming events:
      //   { type:"stream_event", event:{ type:"content_block_delta",
      //       delta:{ type:"text_delta", text:"..." } } }
      // This is the primary streaming path; the cases below handle providers
      // that emit these events unwrapped at the top level.
      const ev = msg.event;
      if (ev?.type === "message_start") {
        // New assistant turn — re-arm so a turn that doesn't stream text
        // (e.g. tool-only) still falls back to its complete-message text.
        state.receivedDelta = false;
      } else if (ev?.type === "content_block_delta" && ev.delta?.type === "text_delta") {
        state.textBuf += ev.delta.text;
        state.receivedDelta = true;
        dbg(`stream_event delta +${ev.delta.text.length}chars, total=${state.textBuf.length}`);
        textChanged = true;
      }
      break;
    }

    case "content_block_delta":
      if (msg.delta?.text) {
        state.textBuf += msg.delta.text;
        state.receivedDelta = true;
        dbg(`cbd +${msg.delta.text.length}chars, total=${state.textBuf.length}`);
        textChanged = true;
      }
      break;

    case "tool_use":
      dbg(`top-level tool_use: ${msg.name}`);
      if (msg.name === "Read" && msg.input?.file_path) {
        const path = msg.input.file_path;
        if (path.includes("reggia")) {
          state.readPages.push(path);
        }
      }
      break;

    case "result":
      dbg(`result, hasContent=${state.hasContent}, textLen=${state.textBuf.length}, denials=${(msg.permission_denials||[]).length}`);
      if (msg.permission_denials?.length) {
        const denied = msg.permission_denials.map(d => d.tool_name || d.tool || "unknown").join(", ");
        state.denials = denied;
      }
      break;

    case "error":
      dbg(`ERROR: ${msg.message}`);
      container.innerHTML = `<div class="msg-error">${escapeHtml(msg.message || "Unknown error")}</div>`;
      state.hasContent = true;
      return;
  }

  // Render — skip when nothing visually changed
  if (!textChanged && !state.denials && state.hasContent) {
    return;
  }

  let html = "";
  if (state.readPages.length > 0) {
    html += `<div class="msg-read-indicator"><i class="ti ti-tool"></i> reading ${state.readPages.join(", ")}</div>`;
  }
  if (state.denials) {
    html += `<div class="msg-denial"><i class="ti ti-lock"></i> blocked: ${state.denials} — add to chat_workspace/.claude/settings.json to allow</div>`;
  }
  if (state.textBuf) {
    html += `<div>${renderMarkdown(state.textBuf)}</div>`;
    state.hasContent = true;
  } else if (!state.denials) {
    // Still waiting for first text — keep the thinking indicator visible
    html = thinkingHtml() + html;
  }
  dbg(`render: hasContent=${state.hasContent}, textLen=${state.textBuf.length}, htmlLen=${html.length}`);
  container.innerHTML = html;
  renderMath(container);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function appendMessage(role, text) {
  const div = document.createElement("div");
  if (role === "user") {
    div.className = "msg-user";
    div.textContent = text;
  } else {
    div.className = "msg-assistant";
    div.innerHTML = text || "";
  }
  getMessagesInner().appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return div;
}

// Ensure the inner clamp wrapper exists inside #chat-messages and return it.
function getMessagesInner() {
  let inner = chatMessages.querySelector(".chat-messages-inner");
  if (!inner) {
    inner = document.createElement("div");
    inner.className = "chat-messages-inner";
    chatMessages.appendChild(inner);
  }
  return inner;
}

function clearMessages() {
  chatMessages.innerHTML = "";
}

function removeEmptyState() {
  const el = chatMessages.querySelector(".msg-empty");
  if (el) el.remove();
}

function showEmptyState() {
  const inner = getMessagesInner();
  if (inner.children.length === 0) {
    inner.innerHTML = '<div class="msg-empty">Ask Reggia anything — it reads your knowledge base to give context-aware answers.</div>';
  }
}

// Configure marked once: hand fenced code blocks to highlight.js so the
// VS Code Light theme classes get applied.
if (typeof marked !== "undefined" && typeof hljs !== "undefined") {
  marked.use({
    renderer: {
      code(token) {
        const raw = typeof token === "string" ? token : (token.text || "");
        const lang = (typeof token === "object" && token.lang) ? token.lang : "";
        let highlighted;
        if (lang && hljs.getLanguage(lang)) {
          highlighted = hljs.highlight(raw, { language: lang, ignoreIllegals: true }).value;
        } else {
          highlighted = hljs.highlightAuto(raw).value;
        }
        const cls = lang ? ` language-${lang}` : "";
        return `<pre><code class="hljs${cls}">${highlighted}</code></pre>`;
      },
    },
  });
}

const KATEX_DELIMS = [
  { left: "$$", right: "$$", display: true },
  { left: "\\[", right: "\\]", display: true },
  { left: "\\(", right: "\\)", display: false },
  { left: "$", right: "$", display: false },
];

function renderMarkdown(text) {
  if (typeof marked !== "undefined") {
    return marked.parse(text);
  }
  return escapeHtml(text).replace(/\n/g, "<br>");
}

// Apply KaTeX auto-render to any LaTeX delimiters found within `el`.
// Safe to call repeatedly while streaming — KaTeX only re-renders new content.
function renderMath(el) {
  if (!el || typeof renderMathInElement === "undefined") return;
  try {
    renderMathInElement(el, { delimiters: KATEX_DELIMS, throwOnError: false });
  } catch (e) {
    // never let a math parse error break the whole message
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Reggia Panel — CRUD
// ---------------------------------------------------------------------------

const reggiaItems = document.getElementById("reggia-items");
const btnAdd = document.getElementById("btn-add-item");

btnAdd.addEventListener("click", () => {
  if (adding) return;
  expandedId = null;
  adding = true;
  render();
});

document.querySelectorAll(".filter-pill").forEach(pill => {
  pill.addEventListener("click", () => {
    currentFilter = pill.dataset.filter;
    expandedId = null;
    adding = false;
    document.querySelectorAll(".filter-pill").forEach(p => p.classList.remove("active"));
    pill.classList.add("active");
    loadItems(currentFilter);
  });
});

document.getElementById("btn-open-notion").addEventListener("click", () => {
  window.open("https://notion.so", "_blank");
});

// Prefer the backend's computed flag; fall back to the same signal the
// overdue badge uses (days_until_due) so the active/past-due split can't
// disagree with the rendered badge, even if is_past_due is missing/stale.
function isItemPastDue(i) {
  if (typeof i.is_past_due === "boolean") return i.is_past_due;
  return i.status === "active" && i.days_until_due != null && i.days_until_due < 0;
}

async function loadItems(status) {
  try {
    // "past due" is a derived view over active items (active + overdue), not a
    // stored status — fetch active and split client-side so the active tab and
    // the past-due tab stay mutually exclusive.
    const apiStatus = status === "past_due" ? "active" : status;
    const url = apiStatus ? `/reggia/items?status=${encodeURIComponent(apiStatus)}` : "/reggia/items";
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(await resp.text());
    items = await resp.json();
    if (status === "active") items = items.filter(i => !isItemPastDue(i));
    else if (status === "past_due") items = items.filter(i => isItemPastDue(i));
    render();
  } catch (err) {
    reggiaItems.innerHTML = `<div class="reggia-loading">Failed to load: ${err.message}</div>`;
  }
}

function render() {
  if (adding) {
    renderAddForm();
    return;
  }

  if (items.length === 0) {
    const msg = currentFilter === "past_due" ? "Nothing past due" : "No items";
    reggiaItems.innerHTML = `<div class="reggia-empty">${msg}</div>`;
    return;
  }

  reggiaItems.innerHTML = items.map(item =>
    item.id === expandedId ? renderExpanded(item) : renderCollapsed(item)
  ).join("");

  attachItemHandlers();

  // Global Esc to collapse
  const escHandler = (e) => {
    if (e.key === "Escape" && expandedId && !adding) {
      expandedId = null;
      render();
      document.removeEventListener("keydown", escHandler);
    }
  };
  if (expandedId) {
    document.addEventListener("keydown", escHandler);
  }
}

function attachItemHandlers() {
  reggiaItems.querySelectorAll(".item-card").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("select") || e.target.closest("[contenteditable]")) return;
      if (card.closest(".items-dimmed-list")) return; // dimmed: not interactive
      expandedId = card.dataset.id;
      render();
    });
  });

  reggiaItems.querySelectorAll(".btn-collapse").forEach(btn => {
    btn.addEventListener("click", () => {
      expandedId = null;
      render();
    });
  });

  reggiaItems.querySelectorAll(".btn-trash").forEach(btn => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      await fetch(`/reggia/items/${id}`, { method: "DELETE" });
      expandedId = null;
      loadItems(currentFilter);
    });
  });

  reggiaItems.querySelectorAll(".btn-ask-reggia").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const name = btn.dataset.name;
      chatInput.value = `Regarding "${name}" — `;
      chatInput.focus();
    });
  });

  reggiaItems.querySelectorAll(".item-select").forEach(sel => {
    sel.addEventListener("change", async (e) => {
      e.stopPropagation();
      const id = sel.dataset.id;
      const field = sel.dataset.field;
      const value = sel.value;
      await fetch(`/reggia/items/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ [field]: value }),
      });
      loadItems(currentFilter);
    });
  });

  reggiaItems.querySelectorAll(".item-expanded-title").forEach(inp => {
    inp.addEventListener("blur", async () => {
      const id = inp.dataset.id;
      const value = inp.value.trim();
      if (value && id) {
        await fetch(`/reggia/items/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: value }),
        });
        loadItems(currentFilter);
      }
    });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { expandedId = null; render(); }
    });
  });

  reggiaItems.querySelectorAll(".priority-pill[data-id]").forEach(pill => {
    pill.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = pill.dataset.id;
      const prio = pill.dataset.priority;
      if (!id || !prio) return;
      await fetch(`/reggia/items/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ priority: prio }),
      });
      loadItems(currentFilter);
    });
  });

  reggiaItems.querySelectorAll(".sensitivity-pill[data-id]").forEach(pill => {
    pill.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = pill.dataset.id;
      const sens = pill.dataset.sensitivity;
      if (!id || !sens) return;
      await fetch(`/reggia/items/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sensitivity: sens }),
      });
      loadItems(currentFilter);
    });
  });

  reggiaItems.querySelectorAll(".item-due-input[data-id]").forEach(inp => {
    inp.addEventListener("change", async () => {
      const id = inp.dataset.id;
      const value = inp.value || null;
      if (id) {
        await fetch(`/reggia/items/${id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ due_date: value }),
        });
        loadItems(currentFilter);
      }
    });
  });

  reggiaItems.querySelectorAll(".notes-content").forEach(div => {
    div.addEventListener("blur", async () => {
      const id = div.dataset.id;
      const value = div.textContent;
      await fetch(`/reggia/items/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes: value }),
      });
    });
  });
}

function renderCollapsed(item) {
  const prio = item.priority || "";
  const prioClass = prio.toLowerCase();
  const pillClass = prioClass === "p0" ? "pill-p0" : prioClass === "p1" ? "pill-p1" : "pill-p2";
  const days = item.days_until_due;
  const deadlineHtml = days !== null && days !== undefined
    ? `<span class="item-deadline${days < 0 ? " overdue" : ""}">${
        days < 0 ? `${-days}d overdue` : days === 0 ? "due today" : `${days}d left`
      }</span>`
    : "";
  const statusLabel = isItemPastDue(item) ? "past due" : (item.status || "");

  return `
    <div class="item-card ${prioClass}" data-id="${item.id}">
      <div class="item-card-row">
        <span class="${pillClass}">${prio || "—"}</span>
        ${deadlineHtml}
      </div>
      <div class="item-name">${escapeHtml(item.name)}</div>
      <div class="item-meta">${item.domain || ""} · ${statusLabel}${item.sensitivity ? ` · ${item.sensitivity}` : ""}</div>
    </div>`;
}

function renderExpanded(item) {
  const prioClass = (item.priority || "").toLowerCase();
  const prio = item.priority || "P2";
  const sens = item.sensitivity || "";

  const prioPill = (val, label) => `
    <span class="priority-pill ${val.toLowerCase()} ${prio === val ? 'selected' : ''}"
          data-id="${item.id}" data-priority="${val}">${label}</span>`;

  const sensLabel = (val, label) => `
    <span class="sensitivity-pill ${val} ${sens === val ? 'sel' : ''}"
          data-id="${item.id}" data-sensitivity="${val}">${label}</span>`;

  return `
    <div class="item-expanded ${prioClass}" data-id="${item.id}">
      <input type="text" class="item-expanded-title" value="${escapeHtml(item.name)}"
             data-id="${item.id}" data-field="name" placeholder="Item title" />

      <div class="item-field-row">
        <div class="item-field-col">
          <div class="item-field-label">Domain</div>
          <select class="item-select" data-id="${item.id}" data-field="domain">
            ${["research","application","work","admin","writing","personal"].map(d =>
              `<option value="${d}" ${item.domain === d ? "selected" : ""}>${d}</option>`
            ).join("")}
          </select>
        </div>
        <div class="item-field-col">
          <div class="item-field-label">Priority</div>
          <div class="priority-pills">
            ${prioPill("P0","P0")}${prioPill("P1","P1")}${prioPill("P2","P2")}${prioPill("P3","P3")}
          </div>
        </div>
      </div>

      <div class="item-field-row">
        <div class="item-field-col">
          <div class="item-field-label">Status</div>
          <select class="item-select" data-id="${item.id}" data-field="status">
            ${["active","pending","completed","dropped"].map(s =>
              `<option value="${s}" ${item.status === s ? "selected" : ""}>${s}</option>`
            ).join("")}
          </select>
        </div>
        <div class="item-field-col">
          <div class="item-field-label">Sensitivity</div>
          <div class="sensitivity-pills">
            ${sensLabel("agent-readable","agent")}${sensLabel("contextual","ctx")}${sensLabel("private","priv")}
          </div>
        </div>
      </div>

      <div class="item-field-label">Due (optional)</div>
      <input type="date" class="item-due-input" value="${item.due_date || ""}"
             data-id="${item.id}" data-field="due_date" />

      <div class="notes-label">Notes</div>
      <div class="notes-content" contenteditable="true" data-id="${item.id}">${escapeHtml(item.notes || "")}</div>

      <div class="item-actions">
        <span class="btn-ask-reggia" data-name="${escapeHtml(item.name)}">Ask Reggia <i class="ti ti-arrow-up-right"></i></span>
        <div class="item-actions-right">
          <span class="item-actions-hint">Esc to collapse</span>
          <span class="btn-trash" data-id="${item.id}" title="Delete item"><i class="ti ti-trash"></i></span>
          <span class="btn-collapse">Collapse</span>
        </div>
      </div>
    </div>`;
}

// Mirrors the template/reggia_create_item_form.html mockup:
// - expanded form at top
// - existing items rendered below, dimmed
function renderAddForm() {
  const formHtml = `
    <div class="item-expanded" style="border-left-color: var(--color-text-info);">
      <input type="text" class="item-expanded-title" id="add-name" placeholder="Item title" autofocus />

      <div class="item-field-row">
        <div class="item-field-col">
          <div class="item-field-label">Domain</div>
          <select class="item-select" id="add-domain">
            ${["research","application","work","admin","writing","personal"].map(d =>
              `<option value="${d}" ${d === "personal" ? "selected" : ""}>${d}</option>`
            ).join("")}
          </select>
        </div>
        <div class="item-field-col">
          <div class="item-field-label">Priority</div>
          <div class="priority-pills" id="add-priority-pills">
            <span class="priority-pill p0" data-p="P0">P0</span>
            <span class="priority-pill p1" data-p="P1">P1</span>
            <span class="priority-pill p2 selected" data-p="P2">P2</span>
            <span class="priority-pill p3" data-p="P3">P3</span>
          </div>
        </div>
      </div>

      <div class="item-field-row">
        <div class="item-field-col">
          <div class="item-field-label">Status</div>
          <select class="item-select" id="add-status">
            <option value="active" selected>active</option>
            <option value="pending">pending</option>
            <option value="completed">completed</option>
          </select>
        </div>
        <div class="item-field-col">
          <div class="item-field-label">Sensitivity</div>
          <div class="sensitivity-pills" id="add-sensitivity-pills">
            <span class="sensitivity-pill agent-readable" data-s="agent-readable">agent</span>
            <span class="sensitivity-pill contextual" data-s="contextual">ctx</span>
            <span class="sensitivity-pill private" data-s="private">priv</span>
          </div>
        </div>
      </div>

      <div class="item-field-label">Due (optional)</div>
      <input type="date" class="item-due-input" id="add-due" />

      <div class="notes-label">Notes (optional)</div>
      <div class="notes-content" id="add-notes" contenteditable="true"></div>

      <div class="item-actions">
        <span class="item-actions-hint">Esc to cancel · Enter to save</span>
        <div class="item-actions-right">
          <button class="btn-collapse" id="btn-cancel-add" style="border:none;background:none;cursor:pointer;">Cancel</button>
          <button class="btn-save-item" id="btn-save-add">Add</button>
        </div>
      </div>
    </div>`;

  const dimmedHtml = items.length > 0
    ? `<div class="items-dimmed-list">${items.map(renderCollapsed).join("")}</div>`
    : "";

  reggiaItems.innerHTML = formHtml + dimmedHtml;

  let addPriority = "P2";
  let addSensitivity = "";
  document.querySelectorAll("#add-priority-pills .priority-pill").forEach(pill => {
    pill.addEventListener("click", () => {
      document.querySelectorAll("#add-priority-pills .priority-pill").forEach(p => p.classList.remove("selected"));
      pill.classList.add("selected");
      addPriority = pill.dataset.p;
    });
  });

  document.querySelectorAll("#add-sensitivity-pills .sensitivity-pill").forEach(pill => {
    pill.addEventListener("click", () => {
      document.querySelectorAll("#add-sensitivity-pills .sensitivity-pill").forEach(p => p.classList.remove("sel"));
      pill.classList.add("sel");
      addSensitivity = pill.dataset.s;
    });
  });

  const nameInput = document.getElementById("add-name");
  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { adding = false; render(); }
    if (e.key === "Enter" && !e.isComposing) saveAddForm(() => addPriority, () => addSensitivity);
  });
  nameInput.focus();

  document.getElementById("btn-cancel-add").addEventListener("click", () => {
    adding = false;
    render();
  });

  document.getElementById("btn-save-add").addEventListener("click", () => saveAddForm(() => addPriority, () => addSensitivity));

  const escHandler = (e) => {
    if (e.key === "Escape" && adding) {
      adding = false;
      render();
      document.removeEventListener("keydown", escHandler);
    }
  };
  document.addEventListener("keydown", escHandler);
}

async function saveAddForm(getPriority, getSensitivity) {
  const name = document.getElementById("add-name")?.value.trim();
  if (!name) return;
  const domain = document.getElementById("add-domain")?.value || undefined;
  const status = document.getElementById("add-status")?.value || undefined;
  const due = document.getElementById("add-due")?.value || undefined;
  const notes = document.getElementById("add-notes")?.textContent.trim() || undefined;
  const priority = getPriority();
  const sensitivity = getSensitivity() || undefined;

  try {
    await fetch("/reggia/items", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, domain, priority, status, due_date: due, notes, sensitivity }),
    });
  } catch (e) {
    // silent — list reload below will reflect server state
  }
  adding = false;
  loadItems(currentFilter);
}

// ---------------------------------------------------------------------------
// Session management
// ---------------------------------------------------------------------------

async function createNewSession() {
  const resp = await fetch("/sessions", { method: "POST" });
  const data = await resp.json();
  sessionId = data.id;
  chatTitle.textContent = "New chat";
  await loadSessions();
  return sessionId;
}

async function loadSessions() {
  try {
    const resp = await fetch("/sessions");
    if (resp.ok) {
      allSessions = await resp.json();
      renderSessionList();
    }
  } catch (e) {
    // keep previous list
  }
}

function renderSessionList() {
  const container = document.getElementById("session-list");
  container.innerHTML = "";

  const searching = !!searchQuery.trim();

  // Search mode
  if (searching) {
    const q = searchQuery.trim().toLowerCase();
    const results = (window._searchResults || []).filter(r => r);

    if (results.length === 0) {
      container.innerHTML = `<div class="session-list-empty">No matches for "${escapeHtml(searchQuery)}"</div>`;
      return;
    }

    results.forEach(r => {
      container.appendChild(buildSessionItem(r, { query: q, withSnippet: !!r.snippet }));
    });
    return;
  }

  // Normal mode — starred first, then the rest (server order preserved within groups)
  if (!allSessions || allSessions.length === 0) {
    container.innerHTML = '<div class="session-list-empty">No chats yet</div>';
    return;
  }

  const starred = allSessions.filter(s => s.starred);
  const unstarred = allSessions.filter(s => !s.starred);
  [...starred, ...unstarred].forEach(s => {
    container.appendChild(buildSessionItem(s));
  });
}

function buildSessionItem(s, opts = {}) {
  const { query = "", withSnippet = false } = opts;

  const item = document.createElement("div");
  item.className = "session-item" + (s.id === sessionId ? " active" : "");
  item.dataset.id = s.id;
  item.title = s.title || "New chat";

  const titleLine = document.createElement("span");
  titleLine.className = "session-item-title-line";
  const titleText = s.title || "New chat";
  const starPrefix = s.starred ? `<i class="ti ti-star-filled session-item-star"></i>` : "";
  titleLine.innerHTML = starPrefix + (query ? highlightMatch(titleText, query) : escapeHtml(titleText));
  item.appendChild(titleLine);

  if (withSnippet && s.snippet) {
    const snippet = document.createElement("span");
    snippet.className = "session-item-snippet";
    snippet.innerHTML = highlightMatch(s.snippet, query);
    item.appendChild(snippet);
  }

  const menuBtn = document.createElement("i");
  menuBtn.className = "ti ti-dots session-item-menu";
  menuBtn.title = "More";
  item.appendChild(menuBtn);

  item.addEventListener("click", (e) => {
    if (e.target === menuBtn) return;
    if (item.querySelector(".session-item-rename-input")) return;
    if (s.id !== sessionId) switchSession(s.id);
  });

  menuBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    openSessionMenu(s, item, menuBtn);
  });

  return item;
}

// Floating session context menu — anchored next to the menu button
let _openMenuSession = null;
let _openMenuItemEl = null;

function openSessionMenu(session, itemEl, anchorEl) {
  _openMenuSession = session;
  _openMenuItemEl = itemEl;
  const menu = document.getElementById("session-menu");
  itemEl.classList.add("menu-open");

  // Update the Star item label
  const starItem = menu.querySelector('[data-action="star"]');
  starItem.querySelector("i").className = session.starred ? "ti ti-star-filled" : "ti ti-star";
  starItem.querySelector("span").textContent = session.starred ? "Unstar" : "Star";

  // Position
  const rect = anchorEl.getBoundingClientRect();
  menu.style.display = "block";
  // measure menu after display
  const menuRect = menu.getBoundingClientRect();
  let top = rect.bottom + 4;
  let left = rect.right - menuRect.width;
  if (left < 8) left = 8;
  if (top + menuRect.height > window.innerHeight - 8) {
    top = rect.top - menuRect.height - 4;
  }
  menu.style.top = top + "px";
  menu.style.left = left + "px";
}

function closeSessionMenu() {
  const menu = document.getElementById("session-menu");
  menu.style.display = "none";
  if (_openMenuItemEl) {
    _openMenuItemEl.classList.remove("menu-open");
  }
  _openMenuSession = null;
  _openMenuItemEl = null;
}

async function toggleStarSession(s) {
  const newVal = !s.starred;
  // optimistic update so the UI reorders immediately
  s.starred = newVal;
  renderSessionList();
  try {
    await fetch(`/sessions/${s.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ starred: newVal }),
    });
  } catch (e) {
    // ignore — server-driven refresh on next load
  }
  loadSessions();
}

function beginRenameSession(s, itemEl) {
  const titleLine = itemEl.querySelector(".session-item-title-line");
  if (!titleLine) return;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "session-item-rename-input";
  input.value = s.title || "";
  input.maxLength = 200;
  // hide title and menu while editing
  titleLine.style.display = "none";
  const menuBtn = itemEl.querySelector(".session-item-menu");
  if (menuBtn) menuBtn.style.display = "none";
  itemEl.insertBefore(input, itemEl.firstChild);
  input.focus();
  input.select();

  let done = false;
  const commit = async () => {
    if (done) return;
    done = true;
    const newTitle = input.value.trim();
    if (newTitle && newTitle !== s.title) {
      s.title = newTitle;
      try {
        await fetch(`/sessions/${s.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: newTitle }),
        });
      } catch (e) { /* ignore */ }
      if (s.id === sessionId) chatTitle.textContent = newTitle;
    }
    loadSessions();
  };
  const cancel = () => {
    if (done) return;
    done = true;
    renderSessionList();
  };

  input.addEventListener("blur", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); input.blur(); }
    else if (e.key === "Escape") { e.preventDefault(); cancel(); }
  });
}

async function deleteSessionConfirmed(s) {
  if (isStreaming && s.id === sessionId) return;
  await fetch(`/sessions/${s.id}`, { method: "DELETE" });
  const wasCurrent = (s.id === sessionId);
  await loadSessions();
  if (searchQuery.trim()) await runSearch(searchQuery);
  if (wasCurrent) {
    sessionId = null;
    clearMessages();
    if (allSessions.length > 0) {
      await switchSession(allSessions[0].id);
    } else {
      await createNewSession();
      showEmptyState();
    }
  }
}

// Highlight matches in a string (case-insensitive). Returns safe HTML.
function highlightMatch(text, q) {
  if (!text || !q) return escapeHtml(text || "");
  const safe = escapeHtml(text);
  const escapedQ = q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(escapedQ, "gi");
  return safe.replace(re, m => `<mark>${m}</mark>`);
}

// Search sessions by query. Tries backend /sessions/search first; if it 404s
// or fails, falls back to client-side title-only search over allSessions.
async function runSearch(q) {
  const query = (q || "").trim();
  if (!query) {
    window._searchResults = [];
    renderSessionList();
    return;
  }

  // Try backend search endpoint
  try {
    const resp = await fetch(`/sessions/search?q=${encodeURIComponent(query)}`);
    if (resp.ok) {
      const data = await resp.json();
      // Expect: [{ id, title, snippet }, ...]
      window._searchResults = Array.isArray(data) ? data : (data.results || []);
      renderSessionList();
      return;
    }
  } catch (e) {
    // fall through to client-side
  }

  // Client-side fallback: title only
  const lower = query.toLowerCase();
  window._searchResults = (allSessions || [])
    .filter(s => (s.title || "").toLowerCase().includes(lower))
    .map(s => ({ id: s.id, title: s.title, snippet: null }));
  renderSessionList();
}

async function switchSession(id) {
  if (id === sessionId) {
    // Same session but user might be in settings view — fall back to chat.
    if (document.getElementById("app").classList.contains("show-settings")) {
      showChatView();
    }
    return;
  }
  if (isStreaming) return; // don't swap mid-stream
  if (document.getElementById("app").classList.contains("show-settings")) {
    showChatView();
  }
  sessionId = id;
  clearMessages();
  try {
    const resp = await fetch(`/sessions/${id}`);
    if (resp.ok) {
      const session = await resp.json();
      chatTitle.textContent = session.title || "New chat";
      (session.messages || []).forEach(m => {
        if (m.role === "user") {
          appendMessage("user", m.content);
        } else {
          const div = appendMessage("assistant", "");
          div.innerHTML = renderMarkdown(m.content);
          renderMath(div);
        }
      });
      if (!session.messages || session.messages.length === 0) {
        showEmptyState();
      }
    }
  } catch (e) {
    showEmptyState();
  }
  renderSessionList();
}

// ---------------------------------------------------------------------------
// Account settings — profile, API keys, avatar
// ---------------------------------------------------------------------------

async function fetchSettings() {
  try {
    const resp = await fetch("/settings");
    if (!resp.ok) return null;
    return await resp.json();
  } catch (e) {
    return null;
  }
}

function applyAccountToSidebar(s) {
  const nameEl = document.getElementById("account-name");
  const avatarEl = document.getElementById("account-avatar");
  if (!s) {
    nameEl.textContent = "Account";
    avatarEl.style.backgroundImage = "";
    avatarEl.innerHTML = '<i class="ti ti-user"></i>';
    return;
  }
  const label = s.display_name || s.user_name || "Account";
  nameEl.textContent = label;
  if (s.avatar_url) {
    avatarEl.style.backgroundImage = `url(${s.avatar_url})`;
    avatarEl.innerHTML = "";
  } else {
    avatarEl.style.backgroundImage = "";
    avatarEl.innerHTML = '<i class="ti ti-user"></i>';
  }
}

function paintAvatarCircle(el, url) {
  if (!el) return;
  if (url) {
    el.style.backgroundImage = `url(${url})`;
    el.innerHTML = "";
  } else {
    el.style.backgroundImage = "";
    el.innerHTML = '<i class="ti ti-user"></i>';
  }
}

function applySettingsToForm(s) {
  userSettings = s || userSettings;
  const heroName = s?.display_name || s?.user_name || "Account";
  document.getElementById("settings-hero-name").textContent = heroName;
  paintAvatarCircle(document.getElementById("settings-hero-avatar"), s?.avatar_url);
  paintAvatarCircle(document.getElementById("avatar-preview"), s?.avatar_url);

  document.getElementById("settings-display-name").value = s?.display_name || "";
  document.getElementById("settings-user-name").value = s?.user_name || "";

  // Reset key inputs to masked placeholder; do NOT prefill the actual key.
  const deepseekInput = document.getElementById("settings-deepseek-key");
  const notionInput = document.getElementById("settings-notion-key");
  deepseekInput.type = "password";
  notionInput.type = "password";
  deepseekInput.value = "";
  notionInput.value = "";
  deepseekInput.placeholder = s?.deepseek_api_key_set ? (s.deepseek_api_key_masked || "•••• set ••••") : "sk-…";
  notionInput.placeholder = s?.notion_api_key_set ? (s.notion_api_key_masked || "•••• set ••••") : "ntn_…";

  document.getElementById("deepseek-key-hint").textContent =
    s?.deepseek_api_key_set ? "A DeepSeek key is configured. Leave blank to keep it; type a new key to replace." : "Not set yet.";
  document.getElementById("notion-key-hint").textContent =
    s?.notion_api_key_set ? "A Notion key is configured. Leave blank to keep it; type a new key to replace." : "Not set yet.";

  // Reset eye icons back to hidden state.
  document.querySelectorAll(".key-toggle i").forEach(i => { i.className = "ti ti-eye"; });
}

function showSettingsView() {
  document.getElementById("app").classList.add("show-settings");
  document.getElementById("settings-pane").style.display = "flex";
  // Refresh in case anything changed
  fetchSettings().then(s => {
    if (s) {
      applySettingsToForm(s);
      applyAccountToSidebar(s);
    }
  });
}

function showChatView() {
  document.getElementById("app").classList.remove("show-settings");
  document.getElementById("settings-pane").style.display = "none";
  chatInput.focus();
}

function setStatus(elId, text, kind = "") {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = text;
  el.classList.remove("ok", "error");
  if (kind) el.classList.add(kind);
}

async function saveProfile() {
  const display = document.getElementById("settings-display-name").value.trim();
  const userName = document.getElementById("settings-user-name").value.trim();

  if (!userName) {
    setStatus("profile-status", "How Reggia calls you can't be empty.", "error");
    return;
  }

  const btn = document.getElementById("btn-save-profile");
  btn.disabled = true;
  setStatus("profile-status", "Saving…");

  try {
    const resp = await fetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: display, user_name: userName }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const updated = await resp.json();
    applySettingsToForm(updated);
    applyAccountToSidebar(updated);
    setStatus("profile-status", "Saved.", "ok");
  } catch (e) {
    setStatus("profile-status", `Failed: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
    setTimeout(() => setStatus("profile-status", ""), 2500);
  }
}

async function saveKeys() {
  const deepseek = document.getElementById("settings-deepseek-key").value.trim();
  const notion = document.getElementById("settings-notion-key").value.trim();

  if (!deepseek && !notion) {
    setStatus("keys-status", "Enter a new value to change a key.", "error");
    return;
  }

  const btn = document.getElementById("btn-save-keys");
  btn.disabled = true;
  setStatus("keys-status", "Saving…");

  const body = {};
  if (deepseek) body.deepseek_api_key = deepseek;
  if (notion) body.notion_api_key = notion;

  try {
    const resp = await fetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const updated = await resp.json();
    applySettingsToForm(updated);
    setStatus("keys-status", "Saved.", "ok");
  } catch (e) {
    setStatus("keys-status", `Failed: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
    setTimeout(() => setStatus("keys-status", ""), 2500);
  }
}

async function toggleKey(field) {
  const inputId = field === "deepseek_api_key" ? "settings-deepseek-key" : "settings-notion-key";
  const input = document.getElementById(inputId);
  const btn = document.querySelector(`.key-toggle[data-field="${field}"]`);
  const icon = btn.querySelector("i");

  // If user has already typed something, just toggle visibility of THAT.
  if (input.value) {
    input.type = input.type === "password" ? "text" : "password";
    icon.className = input.type === "password" ? "ti ti-eye" : "ti ti-eye-off";
    return;
  }

  // Otherwise: if currently hidden, fetch and reveal the stored key.
  if (input.type === "password") {
    try {
      const resp = await fetch("/settings/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ field }),
      });
      if (!resp.ok) throw new Error("reveal failed");
      const data = await resp.json();
      if (!data.value) {
        // Nothing to reveal.
        return;
      }
      input.type = "text";
      input.value = data.value;
      icon.className = "ti ti-eye-off";
    } catch (e) {
      // silent
    }
  } else {
    // hide again — also clear so we don't accidentally re-save the stored value.
    input.type = "password";
    input.value = "";
    icon.className = "ti ti-eye";
  }
}

function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result || "";
      const idx = result.indexOf(",");
      resolve(idx >= 0 ? result.slice(idx + 1) : result);
    };
    reader.onerror = () => reject(new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

async function uploadAvatar(file, statusElId) {
  if (!file) return null;
  if (file.size > 4 * 1024 * 1024) {
    if (statusElId) setStatus(statusElId, "Image is too large (max 4 MB).", "error");
    return null;
  }
  try {
    const data = await readFileAsBase64(file);
    const resp = await fetch("/settings/avatar", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mime: file.type, data }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    return await resp.json();
  } catch (e) {
    if (statusElId) setStatus(statusElId, `Upload failed: ${e.message}`, "error");
    return null;
  }
}

async function removeAvatar() {
  try {
    const resp = await fetch("/settings/avatar", { method: "DELETE" });
    if (!resp.ok) throw new Error(await resp.text());
    return await resp.json();
  } catch (e) {
    return null;
  }
}

function wireSettingsHandlers() {
  document.getElementById("sidebar-account").addEventListener("click", () => {
    showSettingsView();
  });

  document.getElementById("btn-settings-back").addEventListener("click", () => {
    showChatView();
  });

  document.getElementById("btn-save-profile").addEventListener("click", saveProfile);
  document.getElementById("btn-save-keys").addEventListener("click", saveKeys);

  document.querySelectorAll(".key-toggle").forEach(btn => {
    btn.addEventListener("click", () => toggleKey(btn.dataset.field));
  });

  document.getElementById("avatar-file-input").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const updated = await uploadAvatar(file, "profile-status");
    if (updated) {
      applySettingsToForm(updated);
      applyAccountToSidebar(updated);
      setStatus("profile-status", "Avatar updated.", "ok");
      setTimeout(() => setStatus("profile-status", ""), 2500);
    }
    e.target.value = ""; // allow re-uploading same file
  });

  document.getElementById("btn-avatar-remove").addEventListener("click", async () => {
    const updated = await removeAvatar();
    if (updated) {
      applySettingsToForm(updated);
      applyAccountToSidebar(updated);
      setStatus("profile-status", "Avatar removed.", "ok");
      setTimeout(() => setStatus("profile-status", ""), 2500);
    }
  });

  // ESC inside settings view = back to chat
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!document.getElementById("app").classList.contains("show-settings")) return;
    // Don't intercept when an input field is focused so users can clear it.
    const ae = document.activeElement;
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA")) return;
    showChatView();
  });
}

// ---------------------------------------------------------------------------
// First-run welcome modal
// ---------------------------------------------------------------------------

function showWelcomeStep(n) {
  document.querySelectorAll(".welcome-step").forEach(s => {
    s.classList.toggle("active", Number(s.dataset.step) <= n);
  });
  document.querySelectorAll(".welcome-step-pane").forEach(p => {
    p.style.display = Number(p.dataset.step) === n ? "" : "none";
  });
}

function showWelcome() {
  document.getElementById("welcome-overlay").style.display = "flex";
  showWelcomeStep(1);
  // Prefill from current settings if any partial values exist.
  if (userSettings) {
    document.getElementById("welcome-display-name").value = userSettings.display_name || "";
    document.getElementById("welcome-user-name").value = userSettings.user_name || "";
    paintAvatarCircle(document.getElementById("welcome-avatar"), userSettings.avatar_url);
  }
  setTimeout(() => document.getElementById("welcome-user-name").focus(), 60);
}

function hideWelcome() {
  document.getElementById("welcome-overlay").style.display = "none";
}

function wireWelcomeHandlers() {
  document.getElementById("welcome-avatar-input").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const updated = await uploadAvatar(file, "welcome-status");
    if (updated) {
      userSettings = updated;
      paintAvatarCircle(document.getElementById("welcome-avatar"), updated.avatar_url);
      applyAccountToSidebar(updated);
    }
    e.target.value = "";
  });

  document.getElementById("welcome-next").addEventListener("click", async () => {
    const display = document.getElementById("welcome-display-name").value.trim();
    const userName = document.getElementById("welcome-user-name").value.trim();
    if (!userName) {
      setStatus("welcome-status", "Tell Reggia how to address you.", "error");
      return;
    }
    setStatus("welcome-status", "");
    // Save profile fields immediately so refresh doesn't lose them.
    try {
      const resp = await fetch("/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: display, user_name: userName }),
      });
      if (resp.ok) {
        userSettings = await resp.json();
        applyAccountToSidebar(userSettings);
      }
    } catch (e) {
      // best-effort — proceed
    }
    showWelcomeStep(2);
    setTimeout(() => document.getElementById("welcome-deepseek").focus(), 60);
  });

  document.getElementById("welcome-back").addEventListener("click", () => {
    showWelcomeStep(1);
  });

  document.getElementById("welcome-finish").addEventListener("click", async () => {
    const deepseek = document.getElementById("welcome-deepseek").value.trim();
    const notion = document.getElementById("welcome-notion").value.trim();
    if (!deepseek || !notion) {
      setStatus("welcome-status", "Both keys are required to finish setup.", "error");
      return;
    }
    const btn = document.getElementById("welcome-finish");
    btn.disabled = true;
    setStatus("welcome-status", "Saving…");
    try {
      const resp = await fetch("/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ deepseek_api_key: deepseek, notion_api_key: notion }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const updated = await resp.json();
      userSettings = updated;
      applySettingsToForm(updated);
      applyAccountToSidebar(updated);
      if (updated.needs_onboarding) {
        setStatus("welcome-status", "Some fields are still missing.", "error");
        btn.disabled = false;
        return;
      }
      hideWelcome();
    } catch (e) {
      setStatus("welcome-status", `Failed: ${e.message}`, "error");
      btn.disabled = false;
    }
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

async function init() {
  try {
    const resp = await fetch("/chat/config");
    if (resp.ok) {
      const config = await resp.json();
      const select = document.getElementById("chat-model-select");
      config.models.forEach(m => {
        const opt = document.createElement("option");
        opt.value = m;
        opt.textContent = m;
        if (m === config.default_model) opt.selected = true;
        select.appendChild(opt);
      });
      currentModel = config.default_model;
      select.addEventListener("change", () => {
        currentModel = select.value;
      });
    }
  } catch (e) {
    // keep default
  }

  document.getElementById("btn-new-session").addEventListener("click", async () => {
    if (isStreaming) return;
    showChatView();
    await createNewSession();
    clearMessages();
    showEmptyState();
    chatInput.focus();
  });

  // Chat header buttons
  document.getElementById("btn-new-chat-from-header").addEventListener("click", async () => {
    if (isStreaming) return;
    showChatView();
    await createNewSession();
    clearMessages();
    showEmptyState();
    chatInput.focus();
  });

  // Search (always-visible input)
  const searchInput = document.getElementById("sidebar-search-input");
  const searchClear = document.getElementById("sidebar-search-clear");

  function clearSearch() {
    searchQuery = "";
    searchInput.value = "";
    searchClear.style.display = "none";
    window._searchResults = [];
    renderSessionList();
  }

  searchClear.addEventListener("click", () => {
    clearSearch();
    searchInput.focus();
  });

  searchInput.addEventListener("input", () => {
    searchQuery = searchInput.value;
    searchClear.style.display = searchQuery ? "" : "none";
    if (searchDebounce) clearTimeout(searchDebounce);
    if (!searchQuery.trim()) {
      window._searchResults = [];
      renderSessionList();
      return;
    }
    searchDebounce = setTimeout(() => runSearch(searchQuery), 180);
  });

  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      clearSearch();
      searchInput.blur();
    }
  });

  // Collapse / expand sidebar + reggia panel
  const appEl = document.getElementById("app");
  const sidebarHandle = document.getElementById("sidebar-handle");
  const reggiaHandle = document.getElementById("reggia-handle");
  const btnSidebarToggleFromChat = document.getElementById("btn-sidebar-toggle-from-chat");
  const btnReggiaToggleFromChat = document.getElementById("btn-reggia-toggle-from-chat");

  function applyCollapseState() {
    appEl.classList.toggle("sidebar-collapsed", sidebarCollapsed);
    appEl.classList.toggle("reggia-collapsed", reggiaCollapsed);
    sidebarHandle.style.display = sidebarCollapsed ? "" : "none";
    reggiaHandle.style.display = reggiaCollapsed ? "" : "none";
    btnSidebarToggleFromChat.style.display = sidebarCollapsed ? "" : "none";
    btnReggiaToggleFromChat.style.display = reggiaCollapsed ? "" : "none";
    localStorage.setItem("reggia.sidebarCollapsed", sidebarCollapsed ? "1" : "0");
    localStorage.setItem("reggia.reggiaCollapsed", reggiaCollapsed ? "1" : "0");
  }

  document.getElementById("btn-sidebar-collapse").addEventListener("click", () => {
    sidebarCollapsed = true; applyCollapseState();
  });
  sidebarHandle.addEventListener("click", () => {
    sidebarCollapsed = false; applyCollapseState();
  });
  btnSidebarToggleFromChat.addEventListener("click", () => {
    sidebarCollapsed = false; applyCollapseState();
  });

  document.getElementById("btn-reggia-collapse").addEventListener("click", () => {
    reggiaCollapsed = true; applyCollapseState();
  });
  reggiaHandle.addEventListener("click", () => {
    reggiaCollapsed = false; applyCollapseState();
  });
  btnReggiaToggleFromChat.addEventListener("click", () => {
    reggiaCollapsed = false; applyCollapseState();
  });

  applyCollapseState();

  // Session context menu — wire up global handlers
  const sessionMenu = document.getElementById("session-menu");
  sessionMenu.querySelectorAll(".session-menu-item").forEach(it => {
    it.addEventListener("click", async (e) => {
      e.stopPropagation();
      const action = it.dataset.action;
      const s = _openMenuSession;
      const itemEl = _openMenuItemEl;
      closeSessionMenu();
      if (!s) return;
      if (action === "star") {
        await toggleStarSession(s);
      } else if (action === "rename") {
        if (itemEl) beginRenameSession(s, itemEl);
      } else if (action === "delete") {
        await deleteSessionConfirmed(s);
      }
    });
  });

  // Close menu on outside click / Escape
  document.addEventListener("click", (e) => {
    if (sessionMenu.style.display === "none") return;
    if (e.target.closest("#session-menu") || e.target.closest(".session-item-menu")) return;
    closeSessionMenu();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sessionMenu.style.display !== "none") {
      closeSessionMenu();
    }
  });

  // Initialize composer state
  autosizeInput();
  updateSendButtonState();

  // Wire settings & welcome handlers before anything that might surface them.
  wireSettingsHandlers();
  wireWelcomeHandlers();

  // Load user settings — drives sidebar account chip + welcome gating.
  userSettings = await fetchSettings();
  applyAccountToSidebar(userSettings);
  applySettingsToForm(userSettings);
  if (userSettings && userSettings.needs_onboarding) {
    showWelcome();
  }

  await loadSessions();
  // If there are existing chats, open the most recent. Only create new on empty DB.
  if (allSessions.length > 0) {
    await switchSession(allSessions[0].id);
  } else {
    await createNewSession();
    showEmptyState();
  }

  loadItems(currentFilter);
  checkSyncConflicts();
}

// ---------------------------------------------------------------------------
// Longterm-memory sync — conflict modal at boot
// ---------------------------------------------------------------------------

async function checkSyncConflicts() {
  let conflicts = [];
  try {
    const resp = await fetch("/reggia/sync/status");
    if (!resp.ok) return;
    const rows = await resp.json();
    conflicts = (rows || []).filter(r => r.sync_state === "conflict");
  } catch (e) {
    return;
  }
  if (conflicts.length === 0) return;
  renderConflictModal(conflicts);
}

function renderConflictModal(conflicts) {
  let modal = document.getElementById("sync-conflict-modal");
  if (modal) modal.remove();

  modal = document.createElement("div");
  modal.id = "sync-conflict-modal";
  modal.className = "sync-modal-overlay";
  const rowsHtml = conflicts.map(c => {
    const ne = c.notion_last_edited ? new Date(c.notion_last_edited).toLocaleString() : "—";
    const le = c.local_modified_at ? new Date(c.local_modified_at).toLocaleString() : "—";
    return `
      <div class="sync-conflict-row" data-domain="${escapeHtml(c.domain)}">
        <div class="sync-conflict-head">${escapeHtml(c.domain)}</div>
        <div class="sync-conflict-meta">Notion edited: ${escapeHtml(ne)} · Local edited: ${escapeHtml(le)}</div>
        <div class="sync-conflict-actions">
          <button class="sync-btn sync-btn-local"  data-domain="${escapeHtml(c.domain)}" data-winner="local">Keep Local</button>
          <button class="sync-btn sync-btn-notion" data-domain="${escapeHtml(c.domain)}" data-winner="notion">Keep Notion</button>
        </div>
      </div>`;
  }).join("");
  modal.innerHTML = `
    <div class="sync-modal">
      <div class="sync-modal-title">Long-term memory has unresolved conflicts</div>
      <div class="sync-modal-sub">Both Notion and the local cache changed since the last sync. Pick which version to keep per domain.</div>
      ${rowsHtml}
    </div>`;
  document.body.appendChild(modal);

  modal.querySelectorAll(".sync-btn").forEach(btn => {
    btn.addEventListener("click", async () => {
      const domain = btn.dataset.domain;
      const winner = btn.dataset.winner;
      btn.disabled = true; btn.textContent = "Resolving…";
      try {
        await fetch("/reggia/sync/resolve", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ domain, winner }),
        });
      } catch (e) {
        btn.textContent = "Failed — retry";
        btn.disabled = false;
        return;
      }
      const row = modal.querySelector(`.sync-conflict-row[data-domain="${domain}"]`);
      if (row) row.remove();
      if (modal.querySelectorAll(".sync-conflict-row").length === 0) {
        modal.remove();
      }
    });
  });
}

init();
