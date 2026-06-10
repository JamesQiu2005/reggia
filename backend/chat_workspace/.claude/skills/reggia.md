---
name: reggia
description: This skill should be used when the user asks "what should I focus on", asks for planning or prioritization, needs writing or emailing drafted on their behalf, or asks for advice about their work, research, or personal life. Provides curl access to Reggia long-term memory (work, research, intellectual, personal) and active items at http://host.docker.internal:8000. Skip for generic technical or factual questions.
user-invocable: true
allowed-tools:
  - Bash(curl *host.docker.internal*)
---

# Reggia API reference

All endpoints are at `http://host.docker.internal:8000`. Use curl. You do NOT have the Notion API key — the backend is the single gateway.

CLAUDE.md governs *when* to read Reggia and personal preferences. This file documents *how* to talk to the backend.

## Read selectively — do not bulk-fetch

Each curl pulls 5–30K tokens; reading everything wastes the user's budget and dilutes the response. **Hard rules**:

1. **One long-term page per turn** — pick via the routing table below. Only read a second page if the question explicitly spans two domains. Do not read all four "to be safe".
2. **Active items only for action questions** — "what should I focus on", "what's next", deadlines, scheduling, drafting outbound messages. Skip the items endpoint entirely for purely informational questions ("tell me about X").
3. **Prefer `items?domain={X}` over `items?status=active`** — domain-filtered returns a focused subset; the unfiltered active list dumps everything.
4. **Don't re-fetch pages already loaded earlier in the session** — long-term pages and items are stable mid-session unless the user just appended.
5. **Use the exact URL paths documented below.** Valid long-term URLs are `/reggia/index` and `/reggia/longterm/{work|research|intellectual|personal}` only. Never try `/reggia/02%20Work`, `/reggia/05%20Active%20Items`, or other Notion-style names — those return 404. If the index's routing section looks empty or only contains `<!-- reggia:block:... -->` markers, ignore it and use the routing table in this skill instead.

## Long-term memory pages

Markdown content served from a local SQLite cache that the backend two-way-syncs with Notion. Reads are fast (~5 ms) and work offline.

- `curl -s "http://host.docker.internal:8000/reggia/index"` — query routing guide (read first each session)
- `curl -s "http://host.docker.internal:8000/reggia/longterm/work"` — page 01: Work & Academic
- `curl -s "http://host.docker.internal:8000/reggia/longterm/research"` — page 02: Research Trajectory
- `curl -s "http://host.docker.internal:8000/reggia/longterm/intellectual"` — page 03: Intellectual Project
- `curl -s "http://host.docker.internal:8000/reggia/longterm/personal"` — page 04: Personal

## Active items (page 05)

Local SQLite, sorted by urgency, plain JSON.

- `curl -s "http://host.docker.internal:8000/reggia/items?status=active"`
- `curl -s "http://host.docker.internal:8000/reggia/items?status=pending"`
- `curl -s "http://host.docker.internal:8000/reggia/items?domain={domain}"` — filter by `research`, `application`, `work`, `admin`, `writing`, or `personal`

The index page refers to long-term pages by number (01, 02, 03, 04, 05). Use the mapping above to resolve them.

ONLY these 4 long-term pages exist. There is NO long-term page for `admin`, `application`, or `writing` — those are item domains, not long-term pages. Do not invent URLs and do not probe domain names with shell loops; if a domain is not in the list above, it does not exist.

## Response format

- Long-term reads return Markdown with inline formatting (`**bold**`, `*italic*`, `[link](url)`, fenced code, headings, bullets). Parse it as Markdown.
- The HTTP response carries an `X-Reggia-Sync-State` header: `clean` (in sync), `local_dirty` (a local change is queued for the next push), or `conflict` (Notion and local both moved — see failure modes).

## Routing — pick the smallest set

Read **exactly** the rows that match the question. Do not union extras "just in case".

| Question type | Read |
|---|---|
| "what should I focus on" / general planning | `items?status=active` only — no long-term page |
| writing or emailing about work | `longterm/work` + `items?domain=work` |
| research, lab, grad school | `longterm/research` + `items?domain=research` |
| personal life — scheduling, photography, music, family | `longterm/personal` (items only if a deadline is in play) |
| trilogy / philosophical writing | `longterm/intellectual` — no items |
| meta question about the user (background, preferences, how to address them) | usually answer from CLAUDE.md context; skip Reggia |

## Sensitivity

Long-term pages declare sensitivity at the top; items carry a `sensitivity` field.

- `agent-readable`: use freely in any output
- `contextual`: use for reasoning only, never surface in third-party-facing output (emails, drafts to others)
- `private`: skip entirely

## Updating long-term memory

Append to pages 01–04 when the conversation reveals something worth recording: a new project or milestone, a research-direction shift, a skill or tool acquired, a life change that affects scheduling or priorities, or a meaningful change in a lab / professor relationship.

Do NOT propose updates for: trivial corrections, one-off facts already covered by active items, or things mentioned in passing without substance.

```
curl -s -X POST "http://host.docker.internal:8000/reggia/longterm/{domain}" \
  -H "Content-Type: application/json" \
  -d '{"content": "text to append", "type": "bulleted_list_item"}'
```

Supported block types: `paragraph`, `bulleted_list_item`, `heading_3`, `numbered_list_item`. Default to `bulleted_list_item` for factual updates.

Always propose the update explicitly before calling the endpoint, e.g.:

> "I'll add this to your Research page: 'Submitted X paper to Y conference (May 2026)'. OK?"

Only call after the user confirms. Never silently write.

Format for appends:

- Concise and factual. One sentence per bullet.
- Include a date when relevant: `(May 2026)`.
- Don't restate what's already on the page — only append genuinely new information.

## State changes (active items)

If the conversation implies an active item should change (task done, new deadline, status shift), surface it explicitly:

> "Sounds like the grade inquiry is sent. Want me to mark it done?"

Then call `PATCH /reggia/items/{id}` only after the user confirms. Never silently mutate.

## Failure modes

- **HTTP 503** on a GET — local cache is empty for that page (no sync has completed). Tell the user: "long-term memory not synced yet — run `POST /reggia/sync/pull` from the host" and stop.
- **HTTP 409** on a POST `/reggia/longterm/{domain}` — that domain has an unresolved Notion-vs-local conflict. Tell the user: "there's a sync conflict on `<domain>` — please resolve it via the dialog at the top of the Reggia frontend, then I can append" and stop.
- **HTTP 502** on a POST — local write succeeded but the Notion push failed. The line is queued locally. Tell the user: "saved locally but the Notion push failed — the queued change will go up next time you run `POST /reggia/sync/push`."
- **HTTP 502** on a GET — Notion was unreachable for an operation that needed it; rare on reads since they come from SQLite.

## Sync endpoints — do NOT call

`/reggia/sync/status`, `/reggia/sync/pull`, `/reggia/sync/push`, `/reggia/sync/resolve` are administrative endpoints for the user and the frontend. Do not call them yourself. If a sync action is needed, ask the user to trigger it from the frontend.

## Shell-syntax constraint

The backend allowlist permits `curl *host.docker.internal*`. Plain `curl ... "URL"` calls are auto-approved; compound bash (for-loops, conditionals, command substitution) is rejected with "Contains shell syntax (string) that cannot be statically analyzed". Issue one curl per Bash call. Do not loop over guessed domain names — see the fixed list above.
