# Reggia — Personal Knowledge Base

## What this is
Reggia is the user's personal knowledge base, stored in Notion.
It contains long-term context (who they are, what they're working on, their research trajectory, intellectual projects, personal background) and short-term active items (deadlines, pending actions, current priorities).

Read Reggia when the task requires understanding the user's broader context, goals, or current state. Do not read it for purely technical tasks (debugging, math, generic coding) that need no personal context.

## Notion API basics
- Base URL: `https://api.notion.com/v1`
- Auth header: `Authorization: Bearer <NOTION_API_KEY>`
- Version header: `Notion-Version: 2022-06-28`
- Content-Type: `application/json`

The API key is available as the environment variable `NOTION_API_KEY`.

## Reggia structure
Root page ID: `36affd99-02ce-813e-a4c0-d22fdbf4a6c6`

| Page | ID | When to read |
|------|----|--------------|
| 00 Index & Query Guide | `36affd9902ce81ec9f25f5fc438765f3` | Always read first — contains routing rules |
| 01 Work & Academic | `36affd9902ce8191acf6cf6dd3b29eea` | Coursework, CASED, Jarvis-Cockpit |
| 02 Research Trajectory | `36affd9902ce8175b2b2e52fce450723` | Lab outreach, grad apps, research directions |
| 03 Intellectual Project | `36affd9902ce8159bbe2d8b1febf35d6` | Trilogy essays, philosophical frameworks |
| 04 Personal | `36affd9902ce81819e90e0097ceeffe1` | Photography, skiing, music, background |
| 05 Active Items (DB) | `ab0e28e53b474394815913bee3329241` | Current deadlines and pending actions |

## How to read a page
GET `https://api.notion.com/v1/blocks/{page_id}/children?page_size=100`

This returns the page content as blocks. For plain text, concatenate all `rich_text[].plain_text` fields from paragraph and heading blocks.

## How to query Active Items
POST `https://api.notion.com/v1/databases/ab0e28e53b474394815913bee3329241/query`

Filter example — active items only:
```json
{
  "filter": {
    "property": "Status",
    "select": { "equals": "active" }
  }
}
```

After fetching, compute urgency yourself:
- `days_until_due = due_date - today` (negative = overdue)
- `days_since_created = today - created_date`

Sort and prioritize in code, not in the prompt.

## Sensitivity rules
- 🔓 agent-readable: use freely
- 🟡 contextual: use for reasoning, do not surface in third-party outputs (emails, SOPs)
- 🔒 private: skip entirely

## When NOT to read Reggia
- Pure coding / debugging tasks
- Math or algorithms with no personal context needed
- The user has already provided all necessary context in the prompt