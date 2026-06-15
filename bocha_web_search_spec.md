# 博查 Web Search API — Reggia Integration Spec

> **Purpose:** This document tells Claude Code exactly how to implement the `web_search` tool executor in `agent_loop.py`, replacing the current stub at `_exec_web_search()`.

---

## 1. What This Does

The DeepSeek agent calls `web_search(query=...)` as a tool call. The executor posts the query to 博查 (Bocha) Web Search API, extracts the useful fields, and returns a compact string for DeepSeek to synthesize.

## 2. API Contract

```
POST https://api.bocha.cn/v1/web-search
Authorization: Bearer {BOCHA_API_KEY}
Content-Type: application/json
```

### Request Body

```json
{
  "query": "<search query string>",
  "summary": true,
  "freshness": "noLimit",
  "count": 8
}
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `query` | string | yes | The search query, passed through from the tool call argument |
| `summary` | bool | no | `true` = return page summaries (recommended). `false` = snippet only |
| `freshness` | string | no | `"noLimit"` (default, recommended), `"oneDay"`, `"oneWeek"`, `"oneMonth"`, `"oneYear"`, or `"YYYY-MM-DD..YYYY-MM-DD"` |
| `count` | int | no | 1–50, default 10. Use 8 for Reggia — balances coverage vs token cost |
| `include` | string | no | Restrict to specific domains, `\|` or `,` separated. e.g. `"github.com\|arxiv.org"` |
| `exclude` | string | no | Exclude domains, same format |

### Response Structure

```
HTTP 200
```

```json
{
  "code": 200,
  "log_id": "...",
  "msg": null,
  "data": {
    "_type": "SearchResponse",
    "queryContext": {
      "originalQuery": "..."
    },
    "webPages": {
      "totalEstimatedMatches": 8912791,
      "value": [
        {
          "name": "Page title",
          "url": "https://...",
          "snippet": "Short description from search index",
          "summary": "Longer AI-generated summary of the page content (only when summary=true)",
          "siteName": "example.com",
          "datePublished": "2025-02-23T08:18:30+08:00"
        }
      ]
    },
    "images": { "value": [...] },
    "videos": null
  }
}
```

Fields to extract from each item in `data.webPages.value`:

| Field | Use |
|-------|-----|
| `name` | Page title |
| `url` | Source URL (for citation) |
| `summary` | Preferred content field — longer, more informative. Falls back to `snippet` if absent |
| `snippet` | Short description, always present |
| `siteName` | Source site name |
| `datePublished` | Publication date (may be null) |

Ignore `images` and `videos` for now — only `webPages` matters.

### Error Responses

| HTTP Code | `code` | Meaning | Action |
|-----------|--------|---------|--------|
| 400 | 400 | Missing `query` param | Bug in our code — should never happen |
| 401 | 401 | Invalid API key | Check `BOCHA_API_KEY` env var |
| 403 | 403 | Insufficient balance | Return error to model, surface to user |
| 429 | 429 | Rate limited | Retry with backoff, or return error |
| 500 | 500 | Server error | Return error to model |

## 3. Implementation

Replace the stub `_exec_web_search` in `agent_loop.py` with:

```python
BOCHA_SEARCH_URL = "https://api.bocha.cn/v1/web-search"

async def _exec_web_search(query: str) -> str:
    """Search the web via 博查 (Bocha) API. Returns formatted results for the model."""
    api_key = os.environ.get("BOCHA_API_KEY", "")
    if not api_key:
        return json.dumps({"error": "BOCHA_API_KEY not set"})

    payload = {
        "query": query,
        "summary": True,
        "freshness": "noLimit",
        "count": 8,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                BOCHA_SEARCH_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if resp.status_code != 200:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                return json.dumps({
                    "error": f"Bocha API {resp.status_code}",
                    "message": body.get("message", resp.text[:200]),
                })

            data = resp.json()

            if data.get("code") != 200:
                return json.dumps({
                    "error": f"Bocha error {data.get('code')}",
                    "message": data.get("msg", "unknown"),
                })

            pages = (data.get("data") or {}).get("webPages", {}).get("value") or []
            if not pages:
                return json.dumps({"results": [], "note": "No results found."})

            # Format results compactly for the model
            results = []
            for i, p in enumerate(pages, 1):
                content = p.get("summary") or p.get("snippet") or ""
                entry = f"[{i}] {p.get('name', 'Untitled')}"
                if p.get("siteName"):
                    entry += f" ({p['siteName']})"
                if p.get("datePublished"):
                    entry += f" [{p['datePublished'][:10]}]"
                entry += f"\n{content}\nURL: {p.get('url', '')}"
                results.append(entry)

            return "\n\n".join(results)

    except httpx.TimeoutException:
        return json.dumps({"error": "Bocha API timeout (30s)"})
    except Exception as e:
        return json.dumps({"error": f"web_search failed: {str(e)}"})
```

Integrate the current WEB_SEARCH_TOOL tool description with the following manner:

```
"description": (
    "Search the web. The query should be concise search keywords "
    "(2-6 words), not a full sentence. "
    "Example: '博查API 定价' not '博查API的定价是多少钱？'"
),

```

## 4. Environment Variable

Check existing .env satisfies our need or not

## 5. Output Format for DeepSeek

The function returns a plain-text string (not JSON) so DeepSeek can read it naturally:

```
[1] 阿里巴巴发布2024年ESG报告 (alibabagroup.com) [2024-07-22]
阿里巴巴集团发布《2024财年环境、社会和治理（ESG）报告》，详细分享过去一年在ESG各方面取得的进展……
URL: https://www.alibabagroup.com/document-1752073403914780672

[2] 186页｜阿里巴巴：2024年环境、社会和治理（ESG）报告 (搜狐网) [2024-11-07]
报告涵盖了公司在可持续发展方面的多项进展和成就……
URL: https://m.sohu.com/a/815036254_121819701/
```

## 6. Checklist

- [ ] Replace `_exec_web_search` stub with implementation above
- [ ] `BOCHA_API_KEY` to `.env` (already in .env as BOCHA_API_KEY)
- [ ] Add `httpx` timeout handling (already in code above)
- [ ] Test: send a message with web search toggle ON, confirm tool call fires and results come back
- [ ] Verify DeepSeek can synthesize the search results into a coherent answer
