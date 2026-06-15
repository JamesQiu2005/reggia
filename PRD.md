# Reggia 产品需求文档（PRD · 现状版 v0）

> 本文件描述 Reggia **当前已实现**的产品形态（as-is），作为后续功能规划的基线。
> 范围以代码库真实实现为准，与 `README.md` / `agent.md` 的理想化描述如有出入，以本文为准。

---

## 1. 产品概述

**一句话定义：** Reggia 是一个"自己策展记忆"的个人 AI 对话前端——你手动维护结构化的个人上下文，AI 按需读取它来回答和办事，而不是由 AI 去挖掘你的对话来猜你是谁。

**要解决的问题：**
- 主流 LLM 网页端（ChatGPT/Claude/DeepSeek）跨会话会丢失个人上下文；其"记忆"何时调用、调用什么都不可控、不透明。
- 现有记忆框架（Mem0 / Zep / MemPalace / Letta）让 AI 自动抽取并向量化对话，把两类本质不同的上下文混为一谈：**稳定背景**（你是谁、长期在做什么）和 **易变状态**（这周手头有什么）。

**Reggia 的反转主张：** 用户自己定义"什么算信号"。记忆是人写的、人可读的（Notion + 本地 Markdown/SQLite），AI 的职责是**执行**而非**策展**。对应一句设计哲学：*define your own reward function* 应用到 AI 记忆上。

**定位：** 单用户、本地优先（local-first）、可离线读取活跃数据。

---

## 2. 目标用户与使用场景

| | 内容 |
|---|---|
| **核心用户** | 对自己方向和优先级有清晰判断、愿意手写结构化上下文、不希望 AI 挖掘自己对话的个人重度用户（当前即作者本人 Hanze） |
| **典型场景** | 规划/排优先级、代写邮件或文字、关于其工作/科研/生活的建议——任何"答案会因个人上下文而改变"的问题 |
| **不适合** | 想要零维护记忆的人；需要团队/多用户共享上下文；希望 AI 主动捞回你没意识到的旧线索 |

---

## 3. 产品目标与非目标

**目标（Goals）**
- G1 让 AI 在对话中**按需、按域、带敏感度**地读取用户的个人上下文。
- G2 个人记忆**人类可读可编辑**，无向量库、无嵌入管线、无黑箱。
- G3 **长期记忆**（稳定）与**活跃事项**（易变）硬性分离，分别按"稳定上下文"和"实时紧迫度"使用。
- G4 后端是 Notion 的**唯一网关**，对话 Agent 不持有任何 API Key。
- G5 直连 DeepSeek，绕过 Anthropic 中间层；对话 Agent 在 Docker 内做到文件系统隔离。

**非目标（Non-goals）**
- 不做多用户 / 团队协作。
- 不做 AI 自动抽取记忆。
- 不做对抗级安全（敏感度是"君子协定"式的行为护栏，非强制访问控制）。

---

## 4. 核心概念与数据模型

Reggia 的领域模型由四类对象构成，横跨三个 SQLite 库 + Notion。

### 4.1 会话与消息（`reggia_session.db`）
- **sessions**：`id`, `title`(首条消息自动生成，≤6字中文), `created_at`, `updated_at`, `archived`
- **messages**：`id`, `session_id`, `role`(user/assistant), `content`, `created_at`, 以及缓存计量字段 `cache_hit_tokens` / `cache_miss_tokens` / `output_tokens`

### 4.2 活跃事项 Active Items（`reggia_items.db`）
- **items**：`id`, `name`, `domain`, `priority`, `status`, `sensitivity`, `notes`, `due_date`, `created_at`, `archived`
- 取值约定：
  - `priority` ∈ {P0, P1, P2, P3}
  - `status` ∈ {active, pending, completed, dropped}（删除=软删置 dropped）
  - `sensitivity` ∈ {agent-readable, contextual, private}
- **查询时派生字段**（不入库，按当天计算）：`days_until_due`、`days_since_created`、`is_past_due`（= active 且已过 due_date）

### 4.3 长期记忆页 Long-term Pages（`reggia_longterm.db` + Notion）
- 固定 **4 个域**：`work` / `research` / `intellectual` / `personal`，外加一个 `index`（路由导览页）。
- **long_term_memory**：`domain`, `notion_page_id`, `title`, `content_md`, `notion_pending_md`, `notion_last_edited`, `local_modified_at`, `synced_at`, `sync_state`(clean/local_dirty/conflict)
- **block_passthrough**：`domain`, `marker_id`, `raw_json`——存放不支持的 Notion 块以保证往返保真。

### 4.4 敏感度（贯穿活跃事项与长期页）
| 等级 | 含义 |
|---|---|
| `agent-readable` | 可自由使用 |
| `contextual` | 仅供推理，不得出现在面向第三方的输出中 |
| `private` | 完全跳过 |

> ⚠️ 现状不一致点：长期页域固定为 4 个（work/research/intellectual/personal），但活跃事项表单里的 domain 选项是 research/application/work/admin/writing/personal（6 个，与长期页未对齐）。

---

## 5. 功能需求（按模块 · 含现状）

图例：✅ 已实现　🟡 部分/弱实现

### 5.1 对话 Chat
| 功能 | 说明 | 状态 |
|---|---|---|
| 多会话对话 | SSE 流式，后端经 `docker exec` 调用容器内 Claude Code → DeepSeek | ✅ |
| 会话列表 | 新建 / 列表 / 软删归档 | ✅ |
| 会话搜索 | 按标题 + 消息内容搜索，返回片段高亮 | ✅ |
| 收藏 / 重命名 | 星标置顶、手动改名 | ✅ |
| 自动标题 | 首条消息触发轻量 CC 调用生成标题 | ✅ |
| 富文本渲染 | marked.js Markdown + KaTeX 公式 + 代码高亮 + GFM 表格 | ✅ |
| 模型选择 | deepseek-v4-pro[1m] / deepseek-v4-flash | ✅ |
| 停止生成 | 中断当前流 | ✅ |
| 缓存命中统计 | 近 7 天聚合命中率（有 API，UI 暴露弱） | 🟡 |
| 调试日志 | 每会话 `chat_{id}.jsonl`，可列出/读取末 200 行 | ✅ |

### 5.2 知识库 · 活跃事项
| 功能 | 说明 | 状态 |
|---|---|---|
| 事项 CRUD | 面板内快速新增、展开编辑、软删（dropped）、硬删（archive） | ✅ |
| 字段编辑 | 优先级 pill、敏感度 pill、状态/域下拉、due 日期、notes（contenteditable） | ✅ |
| 过滤视图 | active / past due / pending / all 四个 tab（past due 为派生视图） | ✅ |
| 紧迫度计算 | 查询时按当天算 `days_until_due` 与 `is_past_due`，并按到期排序 | ✅ |
| Ask Reggia | 事项卡片一键把上下文预填进对话输入框 | ✅ |

### 5.3 知识库 · 长期记忆与同步
| 功能 | 说明 | 状态 |
|---|---|---|
| 读取长期页 | `/reggia/longterm/{domain}`，SQLite 缓存优先（~5ms，可离线） | ✅ |
| 路由导览 | `/reggia/index`，指导 Agent 该读哪一页 | ✅ |
| 追加块 | `/reggia/longterm/{domain}` 一次请求同时写本地 + Notion | ✅ |
| Notion 双向同步 | 开机后台 pull、脏页 push、冲突检测、冲突解决（local/notion 取舍） | ✅ |
| 同步状态 | 各域 sync_state 查询 | ✅ |
| Markdown 往返 | Notion 块 ↔ Markdown 双向转换（段落/标题/列表/引用/代码/表格 + 块透传） | ✅ |

### 5.4 对话 × 知识库联动
| 功能 | 说明 | 状态 |
|---|---|---|
| Agent 读取 Reggia | 容器内 CC 经 `curl host.docker.internal:8000` 取 index→相关页/事项 | ✅ |
| 敏感度遵守 | 提示词层"君子协定"：agent-readable 自由用、contextual 仅推理、private 跳过 | 🟡（仅提示约束，后端未强制过滤） |
| 写入需确认 | Agent 永不静默写库；提议追加/改状态，用户确认后才调接口 | ✅ |
| 后端单一网关 | 前端与 CC 均无 Notion Key | ✅ |

### 5.5 设置与引导
| 功能 | 说明 | 状态 |
|---|---|---|
| 欢迎/引导流 | 两步弹窗：个人资料 → API Keys | ✅ |
| 账户设置页 | 用户名、头像（base64 上传）、API Key 管理（掩码 + 显示切换） | ✅ |
| Workspace 模板化 | 由 `CLAUDE.md.template` 用 `{USER_NAME}` 渲染，开机及改名时重渲染 | ✅ |

---

## 6. 关键用户流程

**6.1 一次带个人上下文的对话**
```
用户发消息 → 后端载入历史 + 构建缓存优化提示词 → docker exec 调 CC
  → CC 取 /reggia/index（首条消息）→ 按路由取相关长期页/活跃事项
  → 遵守敏感度，把个人上下文揉进回答 → SSE 回流前端渲染
  → 落库消息 + 缓存计量；若首条消息则异步生成标题
```

**6.2 面板管理活跃事项**
```
切换过滤 tab → GET /reggia/items?status= → 计算紧迫度 → 渲染卡片
快速增改删 → POST/PATCH/DELETE → 重新载入
```

**6.3 长期记忆更新（人主导）**
```
对话中 Agent 提议"要不要把 X 记入长期记忆" → 用户确认
  → POST /reggia/longterm/{domain} 同时写本地 + Notion
  → 失败则标 local_dirty，可后续 push 重试
```

---

## 7. 设计原则与约束（产品决策）

1. **用户策展，AI 执行**——记忆由人写，模型不决定什么重要。
2. **长短期硬分离**——长期页稳定、少变；活跃事项带状态/优先级/到期，紧迫度查询时算。
3. **后端是唯一 Notion 网关**——单点审计/限流，Agent 无法外泄凭据。
4. **对话 CC 跑在 Docker 内**——容器边界即安全边界，而非仅靠权限配置。
5. **DeepSeek 直连**——`ANTHROPIC_BASE_URL` 指向 DeepSeek，单 Key，无 OAuth。
6. **缓存友好的三层提示词**——STATIC（CLAUDE.md+技能，恒定）/ STABLE（历史，只追加）/ DYNAMIC（当前消息，仅尾部）。
7. **敏感度为君子协定**——行为护栏，非对抗安全。
8. **chat_workspace 卷挂载**——改 CLAUDE.md/技能即时生效，无需重建镜像。

---

## 8. 技术架构概览

```
浏览器（双栏 UI：对话 | Reggia 面板）
  │  SSE / REST
FastAPI 后端 (:8000，同时托管前端静态资源)
  ├─ sessions/chat → docker exec reggia-cc claude -p … → DeepSeek
  ├─ 活跃事项 → reggia_items.db
  ├─ 长期页 → reggia_longterm.db（缓存）⇄ Notion API
  └─ 设置/同步/日志
```
- 前端：纯 HTML/CSS/JS（无框架），深色主题。
- 运行：`./start.sh`（容器 + 后端一键起；Docker Hub 不可达时降级继续）。
- 分发：`desktop/`（Tauri + PyInstaller 的 macOS 试点打包）。

---

## 9. 当前边界与扩展点（功能 ideation 的发力点）

> 这一节是给"下一步加什么功能"用的——按"现状缺口 → 可能方向"列出。

| # | 现状缺口 | 可能的新功能方向 |
|---|---|---|
| E1 | 对话与知识库**只读单向**：CC 能读 Reggia，但用户无法从对话里把内容沉淀回知识库 | 从对话一键"存入 Reggia"（活跃事项 / 长期页），带来源会话溯源；Agent 主动给出结构化"建议保存"卡片 |
| E2 | 活跃事项是**扁平列表**，无"完成"生命周期、无完成时间、无统计 | completed 状态 + completed_at + Done 视图；按域/周的完成统计 |
| E3 | 缺**主动聚合视图**：is_past_due 算出来了却不主动呈现 | "今日简报"卡片（逾期/今天到期/本周到期/各域计数）；逾期角标 |
| E4 | 敏感度仅在**提示词层**约束，后端对 Agent 仍返回全部（含 private） | 服务端按受众过滤（`audience=agent` 丢 private、脱敏 contextual）+ "Agent 看了什么"审计面板 |
| E5 | 活跃事项与长期页**不连通**：完成一件大事不会沉淀到长期记忆 | 完成事项时可"提升"为长期页的一行摘要 |
| E6 | 搜索只覆盖**会话**，不覆盖事项/长期页 | 知识库全文检索 |
| E7 | 域定义**前后不一致**（长期页 4 域 vs 事项 6 域） | 统一域体系 / 可配置域 |
| E8 | 缓存命中、Token 成本有数据但**无可视化** | 成本/缓存仪表盘 |
| E9 | 无**导出/备份**整个知识库 | 一键导出 Markdown 备份 |
| E10 | 事项无**重复/提醒** | 周期事项、到期提醒/通知 |

---

## 10. 待解决的产品问题（Open Questions）

- Q1 域体系如何统一？是否允许用户自定义域（影响长期页与事项两侧）。
- Q2 "完成"与"丢弃"之外，活跃事项是否需要更细的生命周期（阻塞/等待中）？
- Q3 敏感度要不要从君子协定升级为后端强制？强制后人类 UI 与 Agent 视图如何分流？
- Q4 长期记忆的"沉淀"是否应半自动化（Agent 提议、用户一键确认），还是坚持纯手动？
- Q5 单用户定位是否长期坚持？若开放分发（desktop 试点），多机/同步边界在哪？

---

*文档版本：v0（现状基线）。后续每次重大功能改动应回写本文相关小节。*
