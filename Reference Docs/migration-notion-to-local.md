# Notion → 本地 Markdown 迁移计划

> 目标：将 long-term memory 的正本从 Notion 迁移到本地 Markdown 文件系统。
> 原则：分阶段、CC 路由不中断。具体代码由 Claude Opus 执行，本文只保留方向。

---

## 零、现状梳理：代码分层与处理方式

### 当前数据流

```
Notion (正本) ←→ sync.py (HTTP) ←→ SQLite (缓存) ←→ Backend API ←→ 前端 / CC
```

### 各层职责与迁移处理

CC 访问的读取路径（`GET /reggia/longterm/{domain}`）直接走 `longterm_db.get()`，和 Notion 没有实时依赖——它读的是 SQLite 缓存。Notion 只在后台 sync 和写入时被调用。

**存储层：**

| 数据库 | 当前职责 | 迁后 |
|---|---|---|
| `reggia_longterm.db` | Notion 缓存 + sync_state + passthrough | **整个删掉**。不再需要缓存——文件就是正本，没有网络延迟要规避 |
| `reggia_items.db` | 活跃事项 CRUD | **保留**。结构化字段（priority/status/sensitivity/due_date）需要 SQL 查询 |
| `reggia_session.db` | 会话 + 消息 + token 统计 | **保留**。消息分页、搜索、聚合统计天然适合关系型 |

**代码层：**

| 文件 | 当前职责 | 迁后处理 |
|---|---|---|
| `backend/sync.py` | Notion HTTP 拉取/推送/追加/冲突解决 | **整个删掉**。所有 Notion HTTP 调用集中在此 |
| `backend/notion_markdown.py` | Notion blocks ↔ Markdown 双向转换 | **整个删掉**。block_passthrough 机制随 Notion 一起消失 |
| `backend/test_notion_markdown.py` | 上述的单元测试 | **整个删掉** |
| `backend/longterm_db.py` | SQLite 缓存 + SEED_PAGES + sync_state + passthrough | **整个删掉，替换为 file_store.py** |
| `backend/main.py` | 路由层：调 longterm_db 读，调 sync 写 | **精简**。`_serve_longterm()` 切到 file_store，移除 4 个 sync 路由，移除 lifepsan 中的 pull_all_background |
| `backend/settings.py` | NOTION_API_KEY 管理 | **移除 notion_api_key** 相关逻辑 |
| `frontend/app.js` + `index.html` | Notion key UI + 冲突解决 modal | **移除** Notion UI |
| `backend/chat_workspace/CLAUDE.md.template` | CC 提示词 | **改表述**——Notion → 本地文件 |
| `.claude/skills/reggia.md` | CC 的 Notion API 参考 | **重写**为本地文件 API |
| `desktop/` 下所有文件 | Tauri 中 Notion 设置流程 | **不碰**（desktop/ 已废弃） |

---

## 一、新数据模型

### 存储原则

| 数据类型 | 存储方式 | 原因 |
|---|---|---|
| 长期记忆 | 本地 Markdown 文件（`~/Reggia/longterm/`） | 文档型数据，人需要直接读写，文件系统是正本 |
| 活跃事项 | SQLite（`reggia_items.db`）保留不动 | 结构化记录，需要排序/过滤/派生计算 |
| 会话消息 | SQLite（`reggia_session.db`）保留不动 | 分页/搜索/聚合统计 |

### 文件系统结构

```
~/Reggia/longterm/           ← REGGIA_HOME 环境变量可配，默认 ~/Reggia
├── index.md                 ← 路由导览页（原 /reggia/index）
├── work.md                  ← 原 work 域
├── research.md
├── intellectual.md
├── personal.md
└── .reggia/                 ← Reggia 元数据（不暴露给用户编辑）
    └── metadata.json        ← 文件级别的元数据索引
```

### 文件格式

Obsidian-compatible Markdown（YAML frontmatter + 正文）。不依赖 Obsidian app，任何编辑器都能打开。

```markdown
---
domain: research
title: "LLM Context Management Survey"
tags: [context, llm, survey]
sensitivity: agent-readable
created: 2026-05-20
updated: 2026-06-03
---

## 核心发现
...
```

---

## 二、新增文件

### `backend/file_store.py`（替代 sync.py + longterm_db.py）

职责——只做本地文件 I/O + frontmatter 解析，不做 HTTP 调用，不做格式转换：

- `read(domain)` → Markdown 字符串
- `write(domain, content)` → 写 `.md` 文件 + 更新 metadata.json
- `append(domain, block_content, block_type)` → 按 Markdown 格式追加到文件末尾
- `list_domains()` → 从文件系统扫描 `.md` 文件，自动发现域
- `search(query)` → grep 全文搜索（未来可选 FTS5 索引）
- `get_index()` → 读 index.md

### `backend/reminders_sync.py`（未来，本次迁移不做）

活跃事项 → macOS 系统提醒事项的单向同步：

- 方向：**Reggia → Reminders 单向**。用户在 Reminders 里改的不回写
- 字段映射：`name→title`，`due_date→due date`，`notes→notes`，`priority→priority`（P0→High, P1→Medium, P2/P3→Low）
- 前端：每个事项一个 toggle "同步到系统提醒事项"，默认关闭
- 技术：EventKit bridge（pyobjc 或 subprocess 调 osascript/swift）
- 不重复造轮子：通知时间、重复提醒等配置由用户在 Reminders app 里自己调
- 维护：SQLite 中新增一个 `reminders_id` 字段（可选）

---

## 三、迁移阶段

### Phase 1：数据导出

从现有 SQLite 直接导出 Markdown：`SELECT domain, content_md FROM long_term_memory` → 5 个 `.md` 文件放到 `~/Reggia/longterm/`。

### Phase 2：Backend 读写切换（核心）

1. 新增 `backend/file_store.py`
2. 删除 `backend/longterm_db.py`、`backend/sync.py`、`backend/notion_markdown.py`、`backend/test_notion_markdown.py`
3. 更新 `backend/main.py`：
   - `_serve_longterm()` 从 `longterm_db.get()` 切到 `file_store.read()`，移除 `X-Reggia-Sync-State` header
   - `POST /reggia/longterm/{domain}` 从 `sync.append_block()` 切到 `file_store.append()`
   - 删除 4 个 sync 路由
   - lifepsan 中移除 `sync.pull_all_background()` 和 `longterm_db.init_db()`，替换为 `file_store.ensure_dirs()`
4. 更新 `backend/settings.py`：移除 `notion_api_key`
5. 路由签名保持不变——`GET /reggia/longterm/{domain}` 和 `POST /reggia/longterm/{domain}` 对外行为不变

### Phase 3：CC 侧适配

更新 CLAUDE.md.template 和 skill 文件——把 Notion 表述改为本地文件表述。CC 调用方式不变（还是 curl 同样的路由）。

### Phase 4：前端适配

移除 Notion API key 输入框、冲突解决 modal、sync status 检查。

### Phase 5：清理

移除 Notion 依赖、清理 `.env`、更新文档。不碰 `desktop/`。

---

## 四、不做的

- 不做 Notion 导出工具 UI
- 不做 Obsidian 集成——格式兼容即可
- 不做 bidirectional sync 的本地替代
- 不做迁移回滚机制——Phase 1 导出后 Notion 原始数据就是备份
- **不做活跃事项的 macOS Reminders 同步**——那是下一个独立功能，不在本次迁移范围

---

## 五、风险评估

| 风险 | 缓解 |
|---|---|
| CC 路由中断 | Phase 3 必须验证——发消息确认 CC 能正常读取长期页 |
| Markdown 文件被外部编辑器改坏 | metadata.json 存 SHA256，Backend 检测告警 |
| Docker 卷挂载权限 | `~/Reggia/longterm/` 作为 volume mount |
| 文件名/路径问题 | 文件名用英文（domain 名），正文支持中文 |

---

*最后更新：2026-06-03*
