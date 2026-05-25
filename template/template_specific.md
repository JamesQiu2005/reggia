## Backend 需要新增的 endpoints

GET    /reggia/items?status=active           # list with filter
POST   /reggia/items                         # create
PATCH  /reggia/items/{item_id}               # update any field
DELETE /reggia/items/{item_id}               # archive (set status=dropped, 不真删)

每个 endpoint 内部直接对应一两个 Notion API 调用——POST 是 pages.create，PATCH 是 pages.update，GET 是 databases.query + 字段映射。
一个设计决定：删除我建议不真的删 Notion 页面（数据安全），而是 set Status=dropped，默认 filter 看不到。如果你坚持要"硬删"，加一个 ?hard=true query param。

## 一个 subtle 但重要的点
既然这个面板要支持完整 CRUD，chat agent 通过 conversation 触发的状态变更也要走这套 endpoint，不要让 agent 直接打 Notion API。这样：

1. 状态变更逻辑只有一份（在 backend）
2. 前端的实时刷新可以监听 backend 事件（SSE / WebSocket），agent 改了什么前端立刻看到

