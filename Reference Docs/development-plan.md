# Reggia 开发计划

> 从 web-based 单页应用 → macOS 原生桌面平台的分阶段路线图。
> 原则：每阶段独立可交付、当前运行的应用不中断、个人编码继续用 CC。

---

## Phase 0：地基 —— Backend Notion → 本地文件迁移

**目标**：将长期记忆的正本从 Notion 迁移到本地 Markdown 文件系统。所有上层功能（前端、operator）不感知变化。

**产出：**
- 新增 `backend/file_store.py`
- 删除 `backend/sync.py`、`backend/notion_markdown.py`、`backend/test_notion_markdown.py`
- 删除 `backend/longterm_db.py`（替换为 file_store）
- 更新 `backend/main.py`：路由签名不变，底层切 file_store。移除 4 个 sync 路由和 lifepsan 中的 pull_all_background
- 更新 `backend/settings.py`：移除 notion_api_key
- 更新 CC 的 CLAUDE.md.template + skill 文件：Notion 表述 → 本地文件表述
- 前端：移除 Notion key UI 和冲突解决 modal

**不变：** `GET/POST /reggia/longterm/{domain}` 路由签名和行为。CC 和前端不感知底层切换。

**验证：** 启动 Backend → `GET /reggia/longterm/work` 返回正确 Markdown → 在 Chat 里发"帮我查 work 页" → CC 正常读取回答。

**依赖：** 无。这是最优先的 Phase。

**详细计划：** 见 `migration-notion-to-local.md`。

---

## Phase 1：Web 前端重构（保持浏览器可运行）

**目标：** 在现有 vanilla HTML/CSS/JS 栈上，实现新布局架构：主窗口（Briefing + Library）+ 统一底栏。Chat 暂时仍为二级面板（因为在浏览器里无法做独立窗口）。

**产出：**
- 左侧 48px 图标导航（◆ Briefing / 💬 Chat / 📚 Library）
- Briefing 面：逾期 + 本周 + inline 编辑 + 长期页折叠
- Library 面：长期记忆域列表 + 活跃事项总览
- 统一底栏：输入框 + 五类意图识别 + 结果面板（分组显示）
- Chat 面保留在主窗口内（Phase 3 再独立成窗）
- 活跃事项实体模型在前端落地——同一个对象在 Briefing、Chat、底栏间共享引用

**关键挑战：** 在 vanilla JS 下实现底栏结果面板的键盘导航 + 动画 + 状态管理。保持代码干净，不为将来切框架留债。

**验证：** 浏览器打开 → Briefing 面显示逾期+本周事项 → 点击事项 inline 展开编辑 → 底栏输入 "ask 帮我看看 research 页" → 弹出回答 + 展开到 Chat → Chat 能正常对话。

**依赖：** Phase 0（Backend 路由签名确定后，前端才能对接）。

---

## Phase 2：活跃事项增强 + macOS Reminders 同步

**目标：** 完善活跃事项的生命周期（completed 状态 + completed_at）并实现到 macOS 系统提醒事项的可选同步。

**产出：**
- `reggia_items.db` schema 扩展：`completed_at` 字段
- `backend/reminders_sync.py`：EventKit bridge，Reggia → Reminders 单向同步
- 前端：事项行 toggle "同步到 Reminders"、completed 状态样式（划线）、Done 视图

**验证：** 创建一个事项 → 打开 "同步到 Reminders" → macOS Reminders app 里出现新提醒 → 在 Reggia 里标记完成 → Reminders 里也标记完成（如果实现了双向）。

**依赖：** Phase 1（前端需要 inline 编辑能力）。

---

## Phase 3：Tauri 原生壳

**目标：** 将 web 前端装入 Tauri 壳，实现原生 multi-window Chat、系统快捷键、macOS 原生视觉。

**产出：**
- Tauri 项目初始化（全新 `desktop/` 替换废弃版本）
- 主窗口：系统 webview 渲染 Briefing + Library + 底栏
- Chat 窗口：独立 `Window` 实例，原生 titlebar（close / minimize / fullscreen）
- Cmd+1/2/3 面切换、Cmd+K 聚焦底栏、Cmd+N 新 Chat 窗口
- macOS traffic lights + 毛玻璃背景 + SF Pro 字体
- 多窗口间的状态共享（主窗口改了事项状态 → Chat 窗口即时反映）

**技术要点：**
- Tauri 的 multi-window API：`WebviewWindowBuilder` 创建 Chat 窗口
- Rust 侧事件总线：主窗口和 Chat 窗口通过 `emit` / `listen` 同步状态
- 可以用 Tauri 的 `window-shadows` + `window-vibrancy` plugin 做原生毛玻璃
- Chat 窗口创建时传 context payload（item_id / domain），前端 JS 根据 payload 初始化

**验证：** macOS .app bundle → 双击打开 → 主窗口显示 Briefing → 点击事项 [>] → Chat 独立窗口弹出到当前 Space → ⌘W 关闭 → 主窗口不受影响 → 把 Chat 窗口拖到另一个 Space → 两个窗口独立存在。

**依赖：** Phase 1（前端布局就绪后，Tauri 壳才有东西可装）。

---

## Phase 4：OpenCode 切换

**目标：** 将 operator 从 CC 切换到 OpenCode，验证 Reggia use case 下的表现。

**产出：**
- OpenCode Docker 镜像 / 配置
- opencode.json：agent 路由 + provider fallback chains
- 对比测试：同样的 skill + CLAUDE.md，CC vs OpenCode 在 Reggia 场景下的表现
- 如差距可接受 → 全量切换。如有差距 → 仅 Chat 窗口切，Briefing 聚合继续 CC
- Context Control 插件落地（方案 A 或 B，见 design-tradeoff.md Q1）

**验证：** Chat 窗口里发 "帮我查 research 页关于 LLM context 的笔记" → OpenCode 正确调 Backend API → 读取 Markdown → 回答。粘贴一张图片 → 自动走 vision provider → 正常分析。

**依赖：** Phase 0（Backend 路由是 operator 调用的基础）。Phase 1-3 可以不依赖这个 Phase 先行。

**注意：** 个人编码继续用 CC 命令行，不切。Reggia 的 operator 切换只影响 Reggia 场景。

---

## 依赖关系图

```
Phase 0 (Backend 迁移)
  │
  ▼
Phase 1 (Web 前端重构) ── 可与 Phase 2 部分并行
  │
  ├──► Phase 2 (活跃事项 + Reminders)
  │
  ▼
Phase 3 (Tauri 壳)
  │
  ▼
Phase 4 (OpenCode 切换) ── 可与 Phase 3 并行
```

Phase 0 是地基，必须先做。Phase 1 + 2 可以部分并行（活跃事项的 DB schema 改动能提前做）。Phase 3 等 Phase 1 完成。Phase 4 独立于 Phase 2-3。

---

## 各阶段时间估算

| Phase | 预估 | 关键词 |
|---|---|---|
| Phase 0 | 一个周末（6-8h） | file_store.py、删除 sync、路由不变 |
| Phase 1 | 2-3 个周末 | 布局重写、底栏、实体模型 |
| Phase 2 | 1 个周末 | DB schema、EventKit、前端 toggle |
| Phase 3 | 1-2 个周末 | Tauri 项目、multi-window、快捷键 |
| Phase 4 | 1 个周末 + 持续调优 | OpenCode 配置、对比测试、context 插件 |

---

*最后更新：2026-06-10*
