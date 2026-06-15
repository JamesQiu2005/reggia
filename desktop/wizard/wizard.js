// Reggia first-run wizard.
// The window starts on this page. If a config already exists, we boot services
// and the Rust side navigates the window to http://127.0.0.1:8000 once ready.
// Otherwise, we walk the user through the setup screens.

const { invoke } = window.__TAURI__.tauri;
const { open: openExternal } = window.__TAURI__.shell;

const state = {
  deepseekKey: "",
  notionToken: "",
  parentId: "",
  pageIds: null,
};

// ---------------------------------------------------------------------------
// Screen navigation
// ---------------------------------------------------------------------------

function show(name) {
  for (const el of document.querySelectorAll(".screen")) {
    el.classList.toggle("active", el.id === `screen-${name}`);
  }
}

function setStatus(elId, text, kind) {
  const el = document.getElementById(elId);
  el.textContent = text || "";
  el.className = "status" + (kind ? ` ${kind}` : "");
}

// Click delegation for [data-go] / [data-back] / [data-open]
document.addEventListener("click", (ev) => {
  const t = ev.target;
  if (!(t instanceof HTMLElement)) return;
  if (t.dataset.go) {
    show(t.dataset.go);
    if (t.dataset.go === "docker") checkDocker();
  } else if (t.dataset.back) {
    show(t.dataset.back);
  } else if (t.dataset.open) {
    ev.preventDefault();
    openExternal(t.dataset.open);
  }
});

// ---------------------------------------------------------------------------
// Entry: decide whether to run setup or boot directly
// ---------------------------------------------------------------------------

async function entry() {
  try {
    const cfg = await invoke("cmd_get_config");
    if (cfg) {
      show("done");
      setStatus("done-status", "Starting Docker container and backend…", "pending");
      try {
        await invoke("cmd_start_backend");
        // If we reach here without the window navigating, surface an error.
        setStatus("done-status", "Backend started but window did not navigate. Try restarting Reggia.", "bad");
      } catch (e) {
        setStatus("done-status", `Failed to start: ${e}`, "bad");
      }
      return;
    }
  } catch (_) {
    // no config -> wizard
  }
  show("welcome");
}

// ---------------------------------------------------------------------------
// Docker
// ---------------------------------------------------------------------------

async function checkDocker() {
  setStatus("docker-status", "Checking Docker…", "pending");
  document.getElementById("docker-next").disabled = true;
  document.getElementById("docker-install").hidden = true;
  try {
    const s = await invoke("cmd_check_docker");
    if (s.installed && s.running) {
      setStatus("docker-status", "Docker Desktop is installed and running.", "ok");
      document.getElementById("docker-next").disabled = false;
    } else if (s.installed) {
      setStatus("docker-status", "Docker is installed but not running. Start Docker Desktop, then click Re-check.", "bad");
    } else {
      setStatus("docker-status", "Docker Desktop is not installed.", "bad");
      document.getElementById("docker-install").hidden = false;
    }
  } catch (e) {
    setStatus("docker-status", `Error: ${e}`, "bad");
  }
}

document.getElementById("docker-recheck").addEventListener("click", checkDocker);
document.getElementById("docker-install").addEventListener("click", () => {
  openExternal("https://www.docker.com/products/docker-desktop/");
});
document.getElementById("docker-next").addEventListener("click", () => show("deepseek"));

// ---------------------------------------------------------------------------
// DeepSeek
// ---------------------------------------------------------------------------

document.getElementById("deepseek-validate").addEventListener("click", async () => {
  const key = document.getElementById("deepseek-key").value.trim();
  setStatus("deepseek-status", "Checking…", "pending");
  document.getElementById("deepseek-next").disabled = true;
  const r = await invoke("cmd_validate_deepseek_key", { key });
  if (r.ok) {
    setStatus("deepseek-status", "DeepSeek key accepted.", "ok");
    state.deepseekKey = key;
    document.getElementById("deepseek-next").disabled = false;
  } else {
    setStatus("deepseek-status", r.message, "bad");
  }
});
document.getElementById("deepseek-next").addEventListener("click", () => show("notion-token"));

// ---------------------------------------------------------------------------
// Notion token
// ---------------------------------------------------------------------------

document.getElementById("notion-token-validate").addEventListener("click", async () => {
  const token = document.getElementById("notion-token").value.trim();
  setStatus("notion-token-status", "Checking…", "pending");
  document.getElementById("notion-token-next").disabled = true;
  const r = await invoke("cmd_validate_notion_token", { token });
  if (r.ok) {
    setStatus("notion-token-status", "Notion token accepted.", "ok");
    state.notionToken = token;
    document.getElementById("notion-token-next").disabled = false;
  } else {
    setStatus("notion-token-status", r.message, "bad");
  }
});
document.getElementById("notion-token-next").addEventListener("click", () => show("notion-parent"));

// ---------------------------------------------------------------------------
// Notion parent page
// ---------------------------------------------------------------------------

document.getElementById("notion-parent-validate").addEventListener("click", async () => {
  const urlOrId = document.getElementById("notion-parent").value.trim();
  setStatus("notion-parent-status", "Checking access…", "pending");
  document.getElementById("notion-parent-next").disabled = true;
  const r = await invoke("cmd_validate_parent_page", { token: state.notionToken, urlOrId });
  if (r.ok && r.page_id) {
    setStatus("notion-parent-status", "Parent page accessible.", "ok");
    state.parentId = r.page_id;
    document.getElementById("notion-parent-next").disabled = false;
  } else {
    setStatus("notion-parent-status", r.message, "bad");
  }
});
document.getElementById("notion-parent-next").addEventListener("click", () => show("create"));

// ---------------------------------------------------------------------------
// Create pages
// ---------------------------------------------------------------------------

document.getElementById("create-go").addEventListener("click", async () => {
  document.getElementById("create-go").disabled = true;
  setStatus("create-status", "Creating pages in Notion…", "pending");
  for (const li of document.querySelectorAll("#create-list li")) {
    li.classList.remove("ok", "fail");
  }
  const r = await invoke("cmd_create_notion_pages", {
    token: state.notionToken,
    parentId: state.parentId,
  });
  if (r.ok && r.page_ids) {
    state.pageIds = r.page_ids;
    for (const li of document.querySelectorAll("#create-list li")) {
      li.classList.add("ok");
    }
    setStatus("create-status", "All 5 pages created. Saving configuration…", "ok");
    try {
      await invoke("cmd_save_config", {
        cfg: {
          version: 1,
          deepseek_api_key: state.deepseekKey,
          notion_api_key: state.notionToken,
          notion_page_ids: state.pageIds,
        },
      });
      show("done");
      setStatus("done-status", "Starting services…", "pending");
      try {
        await invoke("cmd_start_backend");
      } catch (e) {
        setStatus("done-status", `Failed to start: ${e}`, "bad");
      }
    } catch (e) {
      setStatus("create-status", `Failed to save config: ${e}`, "bad");
      document.getElementById("create-go").disabled = false;
    }
  } else {
    setStatus("create-status", r.message, "bad");
    document.getElementById("create-go").disabled = false;
  }
});

entry();
