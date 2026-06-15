//! Wizard-time Notion setup: validate tokens, validate the parent page,
//! and create the 5 sub-pages.

use serde::{Deserialize, Serialize};

use crate::config::NotionPageIds;

const NOTION_API: &str = "https://api.notion.com/v1";
const NOTION_VERSION: &str = "2022-06-28";
const DEEPSEEK_API: &str = "https://api.deepseek.com/anthropic/v1/messages";

fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(15))
        .build()
        .expect("build reqwest client")
}

// ---------------------------------------------------------------------------
// DeepSeek key validation
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct KeyCheck {
    pub ok: bool,
    pub message: String,
}

#[tauri::command]
pub async fn cmd_validate_deepseek_key(key: String) -> KeyCheck {
    if key.trim().is_empty() {
        return KeyCheck {
            ok: false,
            message: "Key is empty".into(),
        };
    }
    let body = serde_json::json!({
        "model": "deepseek-v4-flash",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}]
    });
    let resp = client()
        .post(DEEPSEEK_API)
        .header("x-api-key", &key)
        .header("anthropic-version", "2023-06-01")
        .header("authorization", format!("Bearer {key}"))
        .json(&body)
        .send()
        .await;
    match resp {
        Ok(r) if r.status().is_success() => KeyCheck {
            ok: true,
            message: "OK".into(),
        },
        Ok(r) if r.status().as_u16() == 401 => KeyCheck {
            ok: false,
            message: "DeepSeek rejected the key (401)".into(),
        },
        Ok(r) => KeyCheck {
            ok: false,
            message: format!("DeepSeek returned {}", r.status()),
        },
        Err(e) => KeyCheck {
            ok: false,
            message: format!("network error: {e}"),
        },
    }
}

// ---------------------------------------------------------------------------
// Notion token validation
// ---------------------------------------------------------------------------

#[tauri::command]
pub async fn cmd_validate_notion_token(token: String) -> KeyCheck {
    if token.trim().is_empty() {
        return KeyCheck {
            ok: false,
            message: "Token is empty".into(),
        };
    }
    let resp = client()
        .get(format!("{NOTION_API}/users/me"))
        .header("Authorization", format!("Bearer {token}"))
        .header("Notion-Version", NOTION_VERSION)
        .send()
        .await;
    match resp {
        Ok(r) if r.status().is_success() => KeyCheck {
            ok: true,
            message: "OK".into(),
        },
        Ok(r) if r.status().as_u16() == 401 => KeyCheck {
            ok: false,
            message: "Notion rejected the token (401)".into(),
        },
        Ok(r) => KeyCheck {
            ok: false,
            message: format!("Notion returned {}", r.status()),
        },
        Err(e) => KeyCheck {
            ok: false,
            message: format!("network error: {e}"),
        },
    }
}

// ---------------------------------------------------------------------------
// Parent page validation
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct ParentCheck {
    pub ok: bool,
    pub page_id: Option<String>,
    pub message: String,
}

/// Extract the 32-char Notion page ID from any URL or raw ID, and hyphenate.
/// Notion URLs look like https://www.notion.so/Workspace/Page-Title-<32-char-id>
fn parse_page_id(input: &str) -> Option<String> {
    let re = regex::Regex::new(r"[0-9a-fA-F]{32}").ok()?;
    let raw = re.find(input)?.as_str().to_lowercase();
    Some(format!(
        "{}-{}-{}-{}-{}",
        &raw[0..8],
        &raw[8..12],
        &raw[12..16],
        &raw[16..20],
        &raw[20..32]
    ))
}

#[tauri::command]
pub async fn cmd_validate_parent_page(token: String, url_or_id: String) -> ParentCheck {
    let Some(id) = parse_page_id(&url_or_id) else {
        return ParentCheck {
            ok: false,
            page_id: None,
            message: "Could not find a 32-character Notion ID in that input.".into(),
        };
    };
    let resp = client()
        .get(format!("{NOTION_API}/pages/{id}"))
        .header("Authorization", format!("Bearer {token}"))
        .header("Notion-Version", NOTION_VERSION)
        .send()
        .await;
    match resp {
        Ok(r) if r.status().is_success() => ParentCheck {
            ok: true,
            page_id: Some(id),
            message: "OK".into(),
        },
        Ok(r) if r.status().as_u16() == 404 => ParentCheck {
            ok: false,
            page_id: Some(id),
            message: "Notion returned 404 — the integration probably can't see this page yet. \
                     Open the page in Notion, click ··· → Connections → add Reggia, then retry."
                .into(),
        },
        Ok(r) => ParentCheck {
            ok: false,
            page_id: Some(id),
            message: format!("Notion returned {}", r.status()),
        },
        Err(e) => ParentCheck {
            ok: false,
            page_id: None,
            message: format!("network error: {e}"),
        },
    }
}

// ---------------------------------------------------------------------------
// Create the 5 sub-pages
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct CreatePagesResult {
    pub ok: bool,
    pub page_ids: Option<NotionPageIds>,
    pub message: String,
}

#[derive(Debug, Deserialize)]
struct NotionPageResp {
    id: String,
}

async fn create_one(
    client: &reqwest::Client,
    token: &str,
    parent_id: &str,
    title: &str,
) -> Result<String, String> {
    let body = serde_json::json!({
        "parent": { "type": "page_id", "page_id": parent_id },
        "properties": {
            "title": [{"type": "text", "text": {"content": title}}]
        }
    });
    let resp = client
        .post(format!("{NOTION_API}/pages"))
        .header("Authorization", format!("Bearer {token}"))
        .header("Notion-Version", NOTION_VERSION)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("network: {e}"))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("{title}: Notion returned {status}: {text}"));
    }
    let parsed: NotionPageResp = serde_json::from_str(&text)
        .map_err(|e| format!("{title}: parse response: {e}: {text}"))?;
    Ok(parsed.id.replace('-', ""))
}

#[tauri::command]
pub async fn cmd_create_notion_pages(
    token: String,
    parent_id: String,
) -> CreatePagesResult {
    let client = client();
    let titles = [
        ("work", "Work"),
        ("research", "Research"),
        ("intellectual", "Intellectual"),
        ("personal", "Personal"),
        ("index", "Index"),
    ];
    let mut ids = std::collections::HashMap::new();
    for (key, title) in titles {
        match create_one(&client, &token, &parent_id, title).await {
            Ok(id) => {
                ids.insert(key, id);
            }
            Err(e) => {
                return CreatePagesResult {
                    ok: false,
                    page_ids: None,
                    message: e,
                };
            }
        }
    }
    let page_ids = NotionPageIds {
        work: ids.remove("work").unwrap(),
        research: ids.remove("research").unwrap(),
        intellectual: ids.remove("intellectual").unwrap(),
        personal: ids.remove("personal").unwrap(),
        index: ids.remove("index").unwrap(),
    };
    CreatePagesResult {
        ok: true,
        page_ids: Some(page_ids),
        message: "Created 5 pages.".into(),
    }
}
