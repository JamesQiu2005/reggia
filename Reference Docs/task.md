# Reggia — Engineering Task

## The product

**Reggia** is a personal AI chat frontend backed by a user-curated knowledge base. Single-user, local-first.

- **Two-pane UI.** Chat on the left; the **Reggia panel** (a list of *active items* — tasks, deadlines, things on your plate) on the right.
- **Backend:** FastAPI on `:8000` (also serves the frontend). Python 3.12, managed with `uv`.
- **Storage:** SQLite (WAL mode). Active items live in `backend/databases/reggia_items.db`. Long-term pages live in a Notion-backed SQLite cache.
- **Chat:** runs as a Dockerized Claude Code subprocess wired to DeepSeek; it can *read* the knowledge base but is not part of this task.

Every active item has: `name`, `domain` (`work` | `research` | `intellectual` | `personal`), `priority`, `status` (`active` | `dropped`), `sensitivity` (`agent-readable` | `contextual` | `private`), `notes`, `due_date` (ISO `YYYY-MM-DD`), `created_at`.

## Your environment

- The product is **already set up and running**. Start/stop with `./start.sh`. Backend + UI at `http://localhost:8000`.
- Stack: FastAPI + uvicorn (run via `uv`), vanilla HTML/CSS/JS frontend (no framework), SQLite.
- You may use **GitHub Copilot in VSCode** and a **web ChatGPT (GPT‑5.5)** session for review/drafting. Use them freely — but you own the result and must be able to explain any line you ship.

## How this is scored

> 整个测试有个主线任务，建议你先优先完成主线任务，再去修改和完善其他你认为有改进空间的地方。

There is **one main-line task — do it first** and get it working end-to-end. With remaining time, improve anything else you judge worth improving (code quality, UX, edge cases). We weigh: **correctness**, how naturally your code **fits the existing style**, and your **judgment** about what to polish.

Suggested budget: roughly a half-day. Don't gold-plate before the core works.

---

## Main-line task: "Today" briefing + item completion lifecycle

Today the Reggia panel is a flat list of active items. You can create, edit, drop (soft-delete), or hard-delete an item — but you **cannot mark one done**, and there is **no at-a-glance view** of what is overdue or due soon. Add both.

### Part 1 — Completion lifecycle (backend)

Introduce **completion** as a real state, distinct from `dropped` (which means "abandoned", not "finished").

Requirements:
- Add a persisted `completed_at` timestamp to items, and treat `completed` as a first-class `status`.
- **Migration matters:** `reggia_items.db` already exists with live data, and `init_db()` uses `CREATE TABLE IF NOT EXISTS` (so it will *not* add a column to the existing table). Add an **idempotent migration** that adds the column without destroying existing rows.
- Complete an item through the **existing** `PATCH /reggia/items/{id}` (`{"status": "completed"}`). The **server** stamps `completed_at` on the transition into `completed`, and clears it if the item leaves `completed`. Don't add a bespoke endpoint for this.
- Completed items must **drop out of the default active list** but remain queryable via `GET /reggia/items?status=completed`.
- Expose `completed_at` (and, optionally, a derived `days_since_completed`) in the item payload.

Acceptance:
- `PATCH {status:"completed"}` → item gains a server-set `completed_at` and disappears from `?status=active`.
- `GET /reggia/items?status=completed` returns those items.
- Re-activating an item clears `completed_at`.
- Existing items still load after the migration (their `completed_at` is null).

### Part 2 — Briefing endpoint (backend)

Add `GET /reggia/briefing` — a single computed snapshot, derived from **today's date** at request time. Reuse the existing date logic (don't reinvent urgency math). Return these buckets:

| Field | Definition |
|---|---|
| `overdue` | active items with `due_date` before today (`days_until_due < 0`), most-overdue first |
| `due_today` | active, `days_until_due == 0` |
| `due_this_week` | active, `1 <= days_until_due <= 7` |
| `no_due_date` | count of active items with no `due_date` (the silent backlog) |
| `by_domain` | active-item counts per domain |
| `completed_this_week` | items whose `completed_at` is within the last 7 days (count, and/or list) |

Requirements:
- Compute in a single pass over active items; keep it cheap.
- Make "today" injectable (the items layer already supports a `today_override`-style hook) so the buckets are **deterministic and testable**.

Acceptance:
- For a seeded set of items across due dates, every bucket is correct and the counts reconcile with the raw item list.

### Part 3 — "Today" card + complete action (frontend)

- A **Today briefing card** pinned at the top of the Reggia panel, fed by `GET /reggia/briefing`: **Overdue** (visually urgent), **Due today**, **Due this week**, with a small footer like "✓ N done this week". Collapsible, with sensible empty states ("Nothing overdue 🎉"). The buckets must come **from the backend endpoint**, not be recomputed ad hoc in JS.
- A **✓ Complete** affordance on each item card. Reuse the existing inline-action pattern (there is already a drop button and a status control). Completing an item updates the list **and** refreshes the briefing counts.
- A **Done** filter (alongside the existing status filter pills) to view completed items, showing the completed date.
- Match the existing dark theme and component styles — no new framework, no restyle.

Acceptance:
- The card reflects the backend buckets; clicking ✓ completes an item (via `PATCH`), which then leaves the active list and updates the briefing; the Done filter lists completed items.

### Priority within the main-line task

**Core (must work):** Part 1 + Part 3 driven by a real Part 2 endpoint — i.e., a user can complete items and see an accurate Today briefing. If time is short, the `completed_this_week` / `by_domain` extras in Part 2 are the first things to trim.

---

## Where things live (orientation)

**Backend**
- `backend/db.py` — SQLite schema + items CRUD. Key functions: `init_db()`, `list_items(status, domain, today_override)`, `create_item()`, `update_item()` (allowed fields whitelist), `delete_item()`, and `_item_from_row()` — which already derives `days_until_due`, `days_since_created`, and `is_past_due`. **This is your model for the briefing's date math.**
- `backend/main.py` — REST routes: `GET/POST /reggia/items`, `PATCH/DELETE /reggia/items/{id}`. Add `GET /reggia/briefing` here.
- Items DB file: `backend/databases/reggia_items.db`.

**Frontend**
- `frontend/index.html` — Reggia panel markup (right pane).
- `frontend/app.js` — `loadItems(status)`, `render()`, `renderCollapsed()`, `renderExpanded()`, `attachItemHandlers()`, `renderAddForm()`, and the status filter pills.
- `frontend/styles.css` — design tokens + all component styles. Reuse them.

**Tests** — existing pattern in `backend/test_notion_markdown.py` / `backend/test_headless_chat.py`. A small test for the briefing buckets (with a fixed "today") is a strong signal.

---

## Improvement phase (after the main task works)

Pick what you think matters. Candidates:
- **Undo on complete** (toast with an Undo action).
- **Overdue count badge** on the Reggia panel header / sidebar.
- **Relative timestamps** ("2d ago") for completed/created dates.
- Sort the Done view by `completed_at` descending.
- **Timezone correctness** for "today" (the server currently uses local `date.today()`).
- A **test** for the briefing buckets across due-date boundaries.
- Loading / error / empty states for the card.
- Sensitivity-aware briefing *if* it should ever feed the chat agent (exclude `private`).

---

## Wrapping up

- Work on a branch; commit as you go.
- Be ready to **demo it live** and **walk through your diff**.
- Be ready to explain: how you migrated a **populated** DB, how "today"/urgency is computed, and what you deliberately left out and why.
