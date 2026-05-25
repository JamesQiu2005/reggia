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

// ---------------------------------------------------------------------------
// Chat
// ---------------------------------------------------------------------------

const chatMessages = document.getElementById("chat-messages");
const chatInput = document.getElementById("chat-input");
const chatTitle = document.getElementById("chat-title-text");
const btnStopChat = document.getElementById("btn-stop-chat");

chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing && chatInput.value.trim() && !chatInput.disabled) {
    e.preventDefault();
    sendMessage(chatInput.value.trim());
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
  chatInput.disabled = true;
  btnStopChat.style.display = "";
  isStreaming = true;
  removeEmptyState();

  appendMessage("user", prompt);

  const assistantDiv = appendMessage("assistant", "");
  assistantDiv.innerHTML = thinkingHtml();
  const state = { textBuf: "", readPages: [], denials: null, hasContent: false };

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
  chatInput.disabled = false;
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

// state = { textBuf, readPages, denials, hasContent } — mutated in place
function handleStreamMessage(msg, container, state) {
  const dbg = (...args) => console.log("[SSE]", ...args);
  dbg(`type=${msg.type}`, msg.type === "assistant" ? `content_types=${(msg.message?.content||[]).map(b=>b.type).join(",")}` : "");

  switch (msg.type) {
    case "assistant":
      if (msg.message?.content) {
        for (const block of msg.message.content) {
          if (block.type === "text") {
            const added = block.text.length;
            state.textBuf += block.text;
            dbg(`text +${added}chars, total=${state.textBuf.length}`);
          }
          if (block.type === "tool_use") {
            dbg(`tool_use: ${block.name}`);
            if (block.name === "Read") {
              const fp = block.input?.file_path || "";
              if (fp.includes("reggia")) {
                state.readPages.push(fp);
              }
            }
          }
        }
      }
      if (msg.delta?.text) {
        state.textBuf += msg.delta.text;
        dbg(`delta +${msg.delta.text.length}chars`);
      }
      break;

    case "content_block_delta":
      if (msg.delta?.text) {
        state.textBuf += msg.delta.text;
        dbg(`cbd +${msg.delta.text.length}chars`);
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

  // Render
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
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return div;
}

function removeEmptyState() {
  const el = chatMessages.querySelector(".msg-empty");
  if (el) el.remove();
}

function showEmptyState() {
  if (chatMessages.children.length === 0) {
    chatMessages.innerHTML = '<div class="msg-empty">Ask Reggia anything — it reads your knowledge base to give context-aware answers.</div>';
  }
}

function renderMarkdown(text) {
  if (typeof marked !== "undefined") {
    return marked.parse(text);
  }
  return escapeHtml(text).replace(/\n/g, "<br>");
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

async function loadItems(status) {
  try {
    const url = status ? `/reggia/items?status=${encodeURIComponent(status)}` : "/reggia/items";
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(await resp.text());
    items = await resp.json();
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
    reggiaItems.innerHTML = '<div class="reggia-empty">No items</div>';
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
    ? `<span class="item-deadline">${days <= 0 ? "due today" : `${days}d left`}</span>`
    : "";

  return `
    <div class="item-card ${prioClass}" data-id="${item.id}">
      <div class="item-card-row">
        <span class="${pillClass}">${prio || "—"}</span>
        ${deadlineHtml}
      </div>
      <div class="item-name">${escapeHtml(item.name)}</div>
      <div class="item-meta">${item.domain || ""} · ${item.status || ""}</div>
    </div>`;
}

function renderExpanded(item) {
  const prioClass = (item.priority || "").toLowerCase();
  const prio = item.priority || "P2";

  const prioPill = (val, label) => `
    <span class="priority-pill ${val.toLowerCase()} ${prio === val ? 'selected' : ''}"
          data-id="${item.id}" data-priority="${val}">${label}</span>`;

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

      <div class="item-field-label">Due (optional)</div>
      <input type="date" class="item-due-input" value="${item.due_date || ""}"
             data-id="${item.id}" data-field="due_date" />

      <div class="notes-label">Notes</div>
      <div class="notes-content" contenteditable="true" data-id="${item.id}">${escapeHtml(item.notes || "")}</div>

      <div class="item-actions">
        <span class="btn-ask-reggia" data-name="${escapeHtml(item.name)}">Ask Reggia <i class="ti ti-arrow-up-right"></i></span>
        <div class="item-actions-right">
          <span class="item-actions-hint">Esc to collapse</span>
          <i class="ti ti-trash btn-trash" data-id="${item.id}"></i>
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
  document.querySelectorAll("#add-priority-pills .priority-pill").forEach(pill => {
    pill.addEventListener("click", () => {
      document.querySelectorAll("#add-priority-pills .priority-pill").forEach(p => p.classList.remove("selected"));
      pill.classList.add("selected");
      addPriority = pill.dataset.p;
    });
  });

  const nameInput = document.getElementById("add-name");
  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { adding = false; render(); }
    if (e.key === "Enter" && !e.isComposing) saveAddForm(() => addPriority);
  });
  nameInput.focus();

  document.getElementById("btn-cancel-add").addEventListener("click", () => {
    adding = false;
    render();
  });

  document.getElementById("btn-save-add").addEventListener("click", () => saveAddForm(() => addPriority));

  const escHandler = (e) => {
    if (e.key === "Escape" && adding) {
      adding = false;
      render();
      document.removeEventListener("keydown", escHandler);
    }
  };
  document.addEventListener("keydown", escHandler);
}

async function saveAddForm(getPriority) {
  const name = document.getElementById("add-name")?.value.trim();
  if (!name) return;
  const domain = document.getElementById("add-domain")?.value || undefined;
  const due = document.getElementById("add-due")?.value || undefined;
  const notes = document.getElementById("add-notes")?.textContent.trim() || undefined;
  const priority = getPriority();

  try {
    await fetch("/reggia/items", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, domain, priority, due_date: due, notes }),
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

  if (!allSessions || allSessions.length === 0) {
    container.innerHTML = '<div class="session-list-empty">No chats yet</div>';
    return;
  }

  allSessions.forEach(s => {
    const item = document.createElement("div");
    item.className = "session-item" + (s.id === sessionId ? " active" : "");
    item.dataset.id = s.id;
    item.title = s.title || "New chat";

    const label = document.createElement("span");
    label.textContent = s.title || "New chat";
    item.appendChild(label);

    const del = document.createElement("i");
    del.className = "ti ti-x session-item-delete";
    del.title = "Delete chat";
    item.appendChild(del);

    item.addEventListener("click", (e) => {
      if (e.target === del) return;
      if (s.id !== sessionId) switchSession(s.id);
    });

    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (isStreaming && s.id === sessionId) return;
      await fetch(`/sessions/${s.id}`, { method: "DELETE" });
      const wasCurrent = (s.id === sessionId);
      await loadSessions();
      if (wasCurrent) {
        sessionId = null;
        chatMessages.innerHTML = "";
        if (allSessions.length > 0) {
          await switchSession(allSessions[0].id);
        } else {
          await createNewSession();
          showEmptyState();
        }
      }
    });

    container.appendChild(item);
  });
}

async function switchSession(id) {
  if (id === sessionId) return;
  if (isStreaming) return; // don't swap mid-stream
  sessionId = id;
  chatMessages.innerHTML = "";
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
      select.addEventListener("change", async () => {
        currentModel = select.value;
        // Switching model = new session (fresh CC subprocess with new model)
        await createNewSession();
        chatMessages.innerHTML = "";
        showEmptyState();
      });
    }
  } catch (e) {
    // keep default
  }

  document.getElementById("btn-new-session").addEventListener("click", async () => {
    if (isStreaming) return;
    await createNewSession();
    chatMessages.innerHTML = "";
    showEmptyState();
  });

  await loadSessions();
  // If there are existing chats, open the most recent. Only create new on empty DB.
  if (allSessions.length > 0) {
    await switchSession(allSessions[0].id);
  } else {
    await createNewSession();
    showEmptyState();
  }

  loadItems(currentFilter);
}

init();
