# Reggia 设计决策与权衡

> 本文记录架构层面的设计决策、取舍原因、以及尚未解决的开放问题。随讨论更新，不替代 PRD。

---

## 0. 产品定位：聚合平台 vs 嵌入式 AI

### 核心张力

Reggia 和 Apple Intelligence / ChatGPT / 嵌入式 AI 的差异不是能力上的，是**交互范式上的**：

| | 嵌入式 AI（Apple Intelligence） | Reggia |
|---|---|---|
| **入口** | 分散在各 app 里（Mail 里总结、Notes 里重写） | 一个地方，你主动去 |
| **触发方式** | 你在做某事 → 想起来 AI 可以帮忙 → 调用 | 你打开 Reggia → AI 已帮你聚合跨源信息 → 你决策 |
| **AI 的角色** | app 的附加功能 | 跨 app/跨源 的聚合者 |
| **上下文范围** | 当前 app 的当前内容 | 活跃事项 + 长期记忆 + 所有接入的数据源 |
| **用户心智** | "我在写邮件，顺便让 AI 润色" | "我需要理清楚今天该干什么，打开 Reggia" |
| **AI 的存在感** | 等用户召唤 | 用户到之前已经工作了一轮（聚合、计算紧迫度、关联上下文） |

### "聚合式干活"的定义

两个核心动作：

1. **聚合（aggregate）**：把分散在不同地方的信息拉到一起——今天该做什么（活跃事项）、这周发生了什么（会话记录里的决策点）、这个话题我之前怎么看（长期记忆）。AI 做的是**跨源归并**，而不是单点优化。

2. **执行（execute）**：看完之后动手——回消息、改事项状态、把讨论存入长期记忆、让 AI 基于完整上下文写东西。执行动作可能在 Chat 里完成，但**入口不是聊天框**，而是简报里的一个条目。

### 产品姿态

> **Apple Intelligence 说**："你在哪里，AI 就在哪里等你召唤"
> **Reggia 说**："你要管理的事情散落各处，来这里，AI 帮你把它们放在一起"

这个姿态决定了所有下游设计决策：
- 一级界面必须是聚合视图（Briefing），不是聊天框
- Reggia 给人的感觉是"平台"而不是"工具"——是打开它来理事情的，不是需要某个功能时来找的
- 视觉气质偏 macOS 原生 app（Things / Bear / Calendar），而不是网页套壳
- AI 在用户到达之前就已经在运行了——逾期计算、关联推荐、未处理决策点——而不是等用户打字

---

## 1. 架构总览：三层 + 一个薄插件 + 原生壳

```
┌─ Tauri 壳（macOS native）─────────────────────────────┐
│  系统 webview · 原生 title bar · 多窗口 · 快捷键      │
│                                                        │
│   ┌ 主窗口 ───────────────────┐  ┌ Chat 窗口 ────────┐ │
│   │ Briefing + Library + 底栏  │  │ 独立 native window │ │
│   └───────────────────────────┘  └────────────────────┘ │
└────────────────────────────────────────────────────────┘
     │  SSE / REST
     ▼
Backend (:8000) — 数据层
  知识库 API · 敏感度过滤 · 写入回路 · 会话检索 · 文件系统管理 · Reminders 同步
  │
  ▼
Context Control（OpenCode 插件 或 独立 middleware）— 唯一的"薄层"
  三层预算管理 · 透明裁剪 · 摘要生成
  │
  ▼
OpenCode (Docker 内) — operator · 可替换
  opencode.json → model resolution + fallback chains
  oh-my-opencode → agent 路由
  opencode-vision MCP → vision/text 自动 fallback
  │
  ▼
DeepSeek / Anthropic / local Ollama / ...
```

### 为什么 Router 独立进程被取消了

原设计里 Router (:8001) 负责三个模块：

| 模块 | 原 Router 设计 | OpenCode 能做什么 | 结论 |
|---|---|---|---|
| Provider 路由 | 自建 rule-based switch | 四步模型解析 + 75+ provider + 社区插件（oh-my-opencode, opencode-vision MCP） | **不需要自己写** |
| 工具调度 | 不同能力走不同 provider | Agent 体系 + MCP 原生支持 | **不需要自己写** |
| Context 控制 | 三层预算 + 透明裁剪 | ❌ 没有内置 | **这是唯一要自己写的** |

Router 从独立进程退化成**一个 OpenCode 插件（或独立 middleware）+ 一组 opencode.json 配置**。

### Operator 可插拔

架构上 operator 层是**可替换的**，不绑定特定实现：

- **当前**：CC (Docker 内) — 个人使用，成熟稳定，首选 operator
- **未来**：OpenCode (Docker 内) — MIT 协议，分发安全，模型无关

两者的切换不影响 Backend 和前端。

### 为什么桌面端选择 Tauri

- 系统 webview 渲染（现有 HTML/CSS/JS 可大量复用），Rust 层管多窗口、快捷键、系统菜单
- 轻量（~10MB 壳），符合"不做大杂烩"原则
- 原生 multi-window 支持——Chat 作为独立 macOS 窗口（close / minimize / fullscreen）
- 和 `desktop/` 废弃方案的区别：那次是试图把 web-first 设计硬塞进 Tauri。这次是从设计阶段就以 native 为前提

---

## 2. Operator 路由能力：OpenCode 配置化

### Model Resolution（开箱即用）

OpenCode 的四步模型解析：
```
UI 选择 → 用户覆盖 → Provider 回退链 → 系统默认
```

每个 agent 有自己的 provider 回退链。一个挂了自动切下一个。

### Agent 路由（oh-my-opencode 插件）

定义多个专用 agent，各用各的模型：

| Agent | 职责 | 模型 |
|---|---|---|
| Sisyphus | 主 orchestrator | Claude Opus / DeepSeek V4 Pro |
| Oracle | 架构/推理 | GPT-5.4 |
| Explore | 知识库搜索 | Haiku / 免费模型 |
| Librarian | 文档检索 | minimax-free |
| Multimodal Looker | 截图/PDF 分析 | Gemini Flash |

### Vision/Text 自动 Fallback

- Vision-capable model → 图片像素直接传给模型
- Non-vision model → 自动走辅助 vision LLM（如 Qwen2.5-Vision、Gemini Flash）取文字描述 → 传给主模型
- `opencode-vision` MCP server：12 个 vision backend，免费优先 → 付费备用

### 对前端的影响

用户不需要手动选 provider 或 model。在 Chat 窗口粘贴一张图片 → OpenCode 自动判断是否需要 vision model → 透明切换。前端只需要给一个轻量提示（"将使用 vision 模型"），不阻塞发送。

---

## 3. Context Control：仍需自己做的部分

### 问题

随着功能扩展到 Briefing + Chat + 长期记忆，单次 operator 调用的 context 会膨胀。DeepSeek 有 1M token 窗口，但把全部历史+全部知识库塞进去既不经济也不高效。

### 三层策略

| 层 | 内容 | 控制方式 | 管控方 |
|---|---|---|---|
| STATIC | CLAUDE.md + skills（OpenCode 对应 opencode.json + agent 配置） | 启动时加载，不改动 | Backend（模板渲染） |
| STABLE | 历史消息 + 长期记忆 | 最近 N 轮完整，N 轮之前摘要 | **Context Control 插件** |
| DYNAMIC | 当前消息 + 检索到的上下文 | 每轮替换，预算上限 | **Context Control 插件** |

### 预算分配（v1，硬编码）

```
Chat 窗口：DYNAMIC ≤ 60% 窗口
Briefing 聚合：DYNAMIC ≤ 30% 窗口
工具结果缓存：≤ 10% 窗口
```

实现方式见 Q1。

---

## 4. 存储策略：三种数据，三种存储

### 决策矩阵

| 数据类型 | 存储方式 | 原因 |
|---|---|---|
| **长期记忆** | 本地 Markdown 文件（`~/Reggia/longterm/`） | 文档型数据。人需要直接读写。文件是正本。 |
| **活跃事项** | SQLite（`reggia_items.db`）保留不动 | 结构化记录。需要按到期日排序、按状态过滤、计算紧迫度。 |
| **会话消息** | SQLite（`reggia_session.db`）保留不动 | 分页、全文搜索、token 统计聚合。 |

### 长期记忆：本地 Markdown 替代 Notion

Notion 从"正本"降级为"可选的同步目标（未来可能移除）"。本地 Markdown 文件系统成为 long-term memory 的正本。

格式：Obsidian-compatible（YAML frontmatter + Markdown 正文），不依赖 Obsidian app。

`reggia_longterm.db` 整个删除——它的唯一存在理由是为了规避 Notion API 的延迟和 rate limit。迁移到本地文件后，读一个 50KB 的 `.md` 文件是微秒级的，不需要缓存。

迁移计划详见 `migration-notion-to-local.md`。

### 活跃事项的未来：macOS 提醒事项同步

活跃事项目前不和任何外部系统同步。未来同步目标不是 Notion，而是 **macOS 系统提醒事项**：

- Reggia 做结构化策展（优先级、敏感度、域、长期记忆关联）
- Reminders 做到时间喊你（通知、Siri、锁屏、iCloud 跨设备同步）
- 方向：**Reggia → Reminders 单向**。用户自己在 Reminders 里改了不回写
- 前端：每个事项一个 toggle "同步到系统提醒事项"，默认关闭
- 不重复造轮子：通知时间、重复提醒等让用户在 Reminders app 里自己调
- 技术：EventKit bridge（pyobjc 或 subprocess 调 osascript/swift）

### Backend 的职责

Backend 仍然是 operator 和存储之间的唯一网关：
- 按路径/标签读取 Markdown 文件
- 全文搜索（grep + frontmatter 索引）
- 追加/编辑 block
- 活跃事项 CRUD（SQLite）
- Reminders 同步（未来）
- 会话管理（SQLite）

Operator 不直接碰文件系统或 SQLite。

---

## 5. 前端设计：主窗口 + Chat 独立窗口 + 统一底栏

### 5.0 实体模型：活跃事项是 OOP 实体

活跃事项不是列表数据——是**有状态、有行为、可在不同视图间携带上下文的实体**。

```
ActiveItem {
    id, name, domain, priority, status, sensitivity, due_date, notes
    created_at, completed_at
    reminders_id

    .edit()               // 主界面 inline 编辑
    .openInChat()         // 带着全部上下文打开独立 Chat 窗口
    .markComplete()       // 一键完成
    .toggleReminders()    // 同步/取消到 macOS Reminders
    .linkToLongterm()     // 关联一个长期页
}
```

同一个实体出现在三个地方，但记录只有一份（SQLite）：

| 视图 | 显示形态 | 可操作 |
|---|---|---|
| **主窗口 Briefing** | compact row：名称 + P pill + 到期日 + 关联域 | inline 编辑、一键完成、打开 Chat |
| **Chat 窗口** | 窗口标题栏 + 右侧 mini info panel | 对话中改状态、关联长期页 |
| **统一底栏** | 搜索结果中匹配显示 | 一键完成、打开 Chat、导航到 Library |

### 5.1 主窗口布局

```
┌──────────────────────────────────────────────────┐
│  ● ● ●     Reggia                                 │  原生 title bar
├────┬─────────────────────────────────────────────┤
│ ◆  │  Briefing                                    │  左侧：48px 图标导航
│    │  ┌ 逾期（仅在有逾期时显示）────────────────  │  纯图标，无文字
│ 💬 │  │ · 完成 Q2 review · P0 · 昨天到期         │  当前面高亮
│    │  └────────────────────────────────────────── │
│ 📚 │                                              │
│    │  本周                                       │
│    │  · 给导师发邮件 · P0 · 今天 · work  [✓] [>] │  [✓] = 完成
│    │  · 起草 OKR · P1 · 周五 · work     [✓] [>]  │  [>] = 在 Chat 中打开
│    │  · 整理 notes · P2 · 周六          [✓] [>]  │
│    │                                              │
│    │  + 添加事项（inline）                         │
│    │                                              │
│    │  ─────────────────────────────────────────── │
│    │  长期页概览（折叠）                           │
│    │  work · research · intellectual · personal   │
│    │                                              │
├────┴─────────────────────────────────────────────┤
│  > _  ask · search · add · go · remind · export    │  统一底栏，始终可见
└──────────────────────────────────────────────────┘
```

**设计要点：**

- 左侧导航：3 个图标（◆ Briefing / 💬 Chat / 📚 Library）。纯图标，无文字 badge。
- 主区域 = Briefing + 活跃事项合并在一个视图中。长期记忆折叠在底部。
- 逾期区域仅在有逾期时渲染，平时不占空间。
- 每行可 click 展开 inline 编辑（再点收起），不是弹出 modal。
- 双击某事项 → 打开独立 Chat 窗口，带上下文。
- "+ 添加事项" → 行内变成输入框，输入 name → Enter → 创建。

### 5.2 Chat 独立窗口

Chat 是**独立 macOS 原生窗口**——不是主窗口里的 tab 或滑出面板。

```
┌──────────────────────────────────────────────────┐
│  ● ● ●  Chat · 起草 OKR                           │  原生 title bar
│         work · P1 · 周五到期                       │
├──────────────────────────────────────────────────┤
│                                                  │
│              对话区                               │
│                                                  │
│                                                  │
├──────────────────────────────────────────────────┤
│  ┌ 输入消息...                          📎 发送 ┐ │
└──────────────────────────────────────────────────┘
```

**设计要点：**

- 原生 macOS 窗口：close (⌘W)、minimize (⌘M)、全屏 (⌃⌘F)、可拖到不同 Space
- 标题栏显示当前关联的实体（事项名 + 域 + 优先级 + 到期日）。没有关联时只显示 "Chat"
- 主窗口右下角保留小型 💬 Chat 入口按钮，点击 → 打开/激活 Chat 窗口
- Chat 窗口可以从多个入口触发：点击事项 [>]、底栏 ask 展开、Cmd+N 新空白对话

**Chat 的不同入口：**

| 入口 | 带什么上下文 |
|---|---|
| 主窗口点击事项 [>] | 该活跃事项 + 关联的长期页 |
| 底栏 "ask ..." → 展开到 Chat | 问句本身 + 可选的关联 |
| Cmd+N | 空白，无上下文 |
| 从 Library 点击 | 该长期页内容 |

### 5.3 统一底栏（Reggia Spotlight）

位置固定在主窗口底部，始终可见。这是一个**对个人上下文有感知的命令面板**——不是搜索框。

```
空闲态：
┌──────────────────────────────────────────────────┐
│  > _  Type to ask, search, add, or navigate...   │  灰色 placeholder
└──────────────────────────────────────────────────┘

聚焦态 + 结果面板：
┌──────────────────────────────────────────────────┐
│  长期记忆                                        │
│  ┌ research · LLM Context Survey                │
│  │  匹配段落：...cache eviction...               │
│  活跃事项                                        │
│  ┌ 整理 research 笔记 · P2 · 周六               │
│  会话记录                                        │
│  ┌ 06-03 对话 · 「context management 的分类」    │
│  ───────────────────────────────────────────────  │
│  ⏎ Ask Reggia: 「LLM context survey 的最新进展」  │
├──────────────────────────────────────────────────┤
│  > _  LLM context survey                         │  用户输入
└──────────────────────────────────────────────────┘
```

**底栏的五种交互意图（前端 JS 规则识别，不需要 LLM）：**

| 输入模式 | 识别为 | 动作 |
|---|---|---|
| 无前缀，简短关键词 | **搜索** | 弹出结果面板，分组显示 |
| `ask ` / `? ` 开头 / 无前缀完整句子 | **快速问答** | 弹出简短回答 + 「展开到 Chat」入口 |
| `add ` / `+ ` 开头 | **快速添加** | 解析 → 创建活跃事项，底部一闪确认 |
| `go ` / `/ ` 开头 | **导航** | 跳到指定域/面 |
| `remind ` / `export ` 等 | **命令** | 执行系统命令（同步、导出等） |

**结果面板行为：**
- 按数据来源分组：长期记忆 / 活跃事项 / 会话记录
- 每组最多 3 条，超出显示 "查看全部 N 条 →"
- ⏎ 打开第一条结果
- Esc 或点击面板外 → 关闭，回到原界面
- 最底部始终有「Ask Reggia」选项——搜不到满意的，一键变对话

### 5.4 Library 面（长期记忆 + 活跃事项总览）

从左侧 📚 图标进入，或者从底栏搜索结果 "查看全部" 跳转。

```
┌──────────────────────────────────────────────────┐
│  Library    🔍 搜索...（或统一用底栏）             │
├──────────────────────────────────────────────────┤
│  长期记忆                                        │
│  ┌ work · 12 条 · 3 天前更新                     │
│  │ research · 8 条 · 2 天前更新                  │
│  │ intellectual · 5 条 · 1 周前更新              │
│  │ personal · 3 条 · 未更新                      │
│  │ index · 路由页                                │
│                                                  │
│  活跃事项                                        │
│  ┌ active (4) · past due (1) · pending (2)       │
│  │ 每行：名称 + P pill + due date + 域 tag        │
│  │ 点击 → 打开 Chat 窗口                         │
│  │ inline 可编辑                                 │
└──────────────────────────────────────────────────┘
```

---

## 6. 视觉语言

从 OSINT 平台 + macOS 原生应用 + 飞书设计原则的交叉点提取：

| 维度 | 规范 |
|---|---|
| **背景** | 深色毛玻璃（`backdrop-filter: blur`），不是纯黑 |
| **字体** | SF Pro（macOS 系统字体），Regular 为主，Semibold 用于标题 |
| **颜色** | 一个主色调 + 透明度变化。不用多色。逾期用红色 pill，P0/P1/P2 用不同透明度区分 |
| **圆角** | 8px（面板）、6px（按钮）、4px（pill） |
| **间距** | 大面积留白。20 条以内不需要紧凑 |
| **动画** | 面切换用淡入淡出（200ms）。不要弹性动画。不要位移动画 |
| **不做** | emoji 图标、渐变背景、阴影卡片、动画粒子、紫色渐变 |

设计参考来源：
- OSINT 平台（SkyDash、ShadowBroker）→ 聚合视图的图层概念、毛玻璃效果、单屏 snapshot
- 飞书工作台 → 千人千面、渐进式披露、小组件化、模块间通过关联而非堆叠来整合
- macOS 原生（Things 3、Raycast、Spotlight）→ 极简导航 + 统一命令入口

---

## 7. 开发路径

详见 `development-plan.md`。高层方向：

- **Workstream A**：Backend 迁移（Notion → 本地 Markdown）——优先做，其他工作流的基础
- **Workstream B**：前端重做（web 框架内实现新布局 + 统一底栏）
- **Workstream C**：Tauri 壳（多窗口 Chat + 原生快捷键 + 打包）
- **Workstream D**：OpenCode 切换（Docker 内跑通，对比 CC）

个人编码继续用 CC，不分心。

---

## 8. 开放问题

- **Q1** Context Control 的实现方案：OpenCode 插件（方案 A）还是独立 middleware（方案 B）？
- **Q2** ~~desktop native 方案~~ → **已关闭**。Tauri + 系统 webview + 原生多窗口。随前端开发进度并行启动。
- **Q3** 长期记忆的结构由用户通过文件夹定义。Library 面如何渲染用户自定义的文件夹层级？需要多少 UI 约定（index.md 作为每个文件夹的导览）？
- **Q4** 敏感度（agent-readable / contextual / private）从 prompt 层君子协定升级为 Backend 服务端过滤。过滤发生在 API 层还是 Context Control 层？
- **Q5** 写入回路（E1）：Chat 里被标记为"值得保留"的东西，走什么流程写入长期记忆？operator 提议 → 前端展示预览 → 用户确认 → Backend 写文件？
- **Q6** OpenCode 的 `oh-my-opencode` 是社区插件，是否存在被 Anthropic 施压下架的风险？如有，fallback 方案是什么？

---

## 9. 未纳入的设计（刻意不做）

| 方向 | 不采纳原因 |
|---|---|
| CC 直接接入多个 provider | operator 只应看到一个 endpoint。provider 切换是 infra 层的事 |
| 自建 Router 独立进程做 provider 路由 | OpenCode 原生 + 插件生态已覆盖此能力，自建是重复造轮子 |
| 继续用 Notion 作为 long-term 正本 | rate limit、离线不可写、分发硬依赖、域结构被 page 模型锁死 |
| LLM-based provider routing | OpenCode 的 rule + agent 路由够用，额外 LLM 增加延迟和成本 |
| 前端从 Chat 开始（Chat 作为一级界面） | 用户启动 Reggia 的核心意图是"知道今天该做什么"而非"和 AI 聊天" |
| 复用 `desktop/` 的 Tauri 方案 | 已废弃，架构已变 |
| 个人编码场景也切到 OpenCode | CC 在编程场景下仍然是更好的选择，不折腾 |
| 瀑布流 / 无限滚动 / feed 布局 | 个人内容量不需要。一屏装得下 |
| 仪表盘式图表堆砌 | 不是数据量足够做图表的产品 |
| 浏览器 window.open() 模拟 Chat 窗口 | 不可控、无法独立进 Mission Control、没有原生 titlebar |

---

*最后更新：2026-06-10 · 随架构讨论持续更新*
