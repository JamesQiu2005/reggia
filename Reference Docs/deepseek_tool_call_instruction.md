# DeepSeek API Tool Call — Integration Reference

> 目标读者：AI coding agent（如 Claude Code）。本文档描述如何在 Reggia 的 LLM orchestrator 中集成 DeepSeek API 的 Tool Call 能力，替代原先的 Claude Code / OpenCode wrapper。

---

## 1. 核心概念

DeepSeek 的 Tool Call 机制与 OpenAI function calling 完全兼容（同一套 schema）。

**关键约束：模型不执行函数。** 它只生成调用意图和参数，实际执行在 orchestrator 侧完成，结果再回传。整个过程是一个同步的 request-response 来回，不是持久连接。

**完整流程（必须严格遵守轮次顺序）：**

```
Round 1: user message + tools 定义 → 模型返回 tool_calls
Round 2: 执行函数 → 把结果以 role: "tool" 追加进 messages → 重新请求
Round 3: 模型返回最终自然语言回答（finish_reason: "stop"）
```

多工具调用时，Round 2 可能有多个 tool message，但必须全部回传后再发起 Round 3 请求。

---

## 2. Request 结构

### Endpoint

```
POST https://api.deepseek.com/chat/completions
```

Strict 模式（Beta）：
```
POST https://api.deepseek.com/beta/chat/completions
```

### 关键字段

```json
{
  "model": "deepseek-v4-pro",
  "messages": [...],
  "tools": [...],
  "tool_choice": "auto"
}
```

**`tool_choice` 可选值：**

| 值 | 行为 |
|----|------|
| `"none"` | 强制不调用任何工具，直接回文字 |
| `"auto"` | 模型自行决定（有 tools 时的默认值） |
| `"required"` | 强制必须调用至少一个工具 |
| `{"type": "function", "function": {"name": "fn_name"}}` | 强制调用指定工具 |

### Tool 定义格式

```json
{
  "type": "function",
  "function": {
    "name": "get_weather",
    "description": "获取指定城市的实时天气。用户需先提供城市名。",
    "parameters": {
      "type": "object",
      "properties": {
        "location": {
          "type": "string",
          "description": "城市名，如 '杭州' 或 'San Francisco, CA'"
        }
      },
      "required": ["location"]
    }
  }
}
```

**命名规则：** `name` 只能包含 `[a-zA-Z0-9_-]`，最长 64 字符。最多传入 128 个 tool。

---

## 3. Response 解析

### 判断是否需要调用工具

```python
response = client.chat.completions.create(...)
message = response.choices[0].message

if message.tool_calls:
    # 需要执行工具
    for tool_call in message.tool_calls:
        fn_name = tool_call.function.name
        # arguments 是字符串，必须 json.loads()
        import json
        args = json.loads(tool_call.function.arguments)
        tool_call_id = tool_call.id
```

### finish_reason 枚举

| 值 | 含义 |
|----|------|
| `"tool_calls"` | 模型要调用工具，继续 Round 2 |
| `"stop"` | 正常结束，取 `message.content` |
| `"length"` | 被 max_tokens 截断 |
| `"content_filter"` | 内容被过滤 |
| `"insufficient_system_resource"` | 推理资源不足，需重试 |

---

## 4. 完整多轮示例（Python）

```python
from openai import OpenAI
import json

client = OpenAI(
    api_key="<DEEPSEEK_API_KEY>",
    base_url="https://api.deepseek.com"
)

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "获取指定城市的实时天气。",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "城市名"}
            },
            "required": ["location"]
        }
    }
}]

def execute_tool(name: str, args: dict) -> str:
    """实际的工具执行层，由 orchestrator 实现"""
    if name == "get_weather":
        # 调用真实天气 API
        return f"24°C，晴，湿度 60%"
    raise ValueError(f"Unknown tool: {name}")

def run_agent(user_input: str) -> str:
    messages = [{"role": "user", "content": user_input}]

    while True:
        response = client.chat.completions.create(
            model="deepseek-v4-pro",
            messages=messages,
            tools=tools
        )
        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        if finish_reason == "stop":
            return msg.content

        if finish_reason == "tool_calls":
            # 把 assistant 消息追加进历史（含 tool_calls 字段）
            messages.append(msg)

            # 执行所有工具调用，逐一追加结果
            for tool_call in msg.tool_calls:
                args = json.loads(tool_call.function.arguments)
                result = execute_tool(tool_call.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result
                })
            # 继续循环，发起下一轮请求

        elif finish_reason == "insufficient_system_resource":
            raise RuntimeError("DeepSeek 推理资源不足，请重试")
        else:
            raise RuntimeError(f"Unexpected finish_reason: {finish_reason}")

print(run_agent("杭州今天天气怎么样？"))
```

---

## 5. Strict 模式（Beta）

严格模式下，模型输出的函数调用参数保证符合你定义的 JSON Schema，不会幻觉出 schema 以外的字段。

**启用条件（三项缺一不可）：**
1. `base_url` 改为 `https://api.deepseek.com/beta`
2. 每个 function 设置 `"strict": true`
3. 所有 `object` 类型：全部属性必须在 `required` 里，且设 `"additionalProperties": false`

```json
{
  "type": "function",
  "function": {
    "name": "create_task",
    "strict": true,
    "parameters": {
      "type": "object",
      "properties": {
        "title": {"type": "string"},
        "priority": {
          "type": "string",
          "enum": ["low", "medium", "high"]
        }
      },
      "required": ["title", "priority"],
      "additionalProperties": false
    }
  }
}
```

### Strict 模式支持的 JSON Schema 类型

| 类型 | 说明 | 不支持的字段 |
|------|------|-------------|
| `object` | 所有属性必须在 `required`，需设 `additionalProperties: false` | — |
| `string` | 支持 `pattern`、`format`（email/hostname/ipv4/ipv6/uuid） | `minLength`、`maxLength` |
| `number` / `integer` | 支持 `minimum`、`maximum`、`multipleOf` 等 | — |
| `array` | 支持 `items` | `minItems`、`maxItems` |
| `boolean` | — | — |
| `enum` | 确保输出是枚举值之一 | — |
| `anyOf` | 匹配多个 schema 之一 | — |
| `$ref` / `$def` | 模块化引用，支持递归结构 | — |

---

## 6. 思考模式下的 Tool Call

DeepSeek-V3.2 起支持。Response 中会多一个 `reasoning_content` 字段，记录调用前的推理过程。

```python
response = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=messages,
    tools=tools,
    thinking={"type": "enabled"}  # 可选，默认 enabled
)

msg = response.choices[0].message
# msg.reasoning_content — 推理链（仅思考模式）
# msg.tool_calls — 工具调用意图
# msg.content — 最终文字回答
```

---

## 7. 常见陷阱

| 陷阱 | 说明 |
|------|------|
| `arguments` 是字符串 | 必须 `json.loads(tool_call.function.arguments)`，不是 dict |
| 模型可能幻觉参数 | 即使不用 strict 模式，执行前也要校验参数合法性 |
| assistant 消息必须先追加 | Round 2 之前必须把 `msg`（含 tool_calls）追加进 messages，否则 API 报错 |
| 所有 tool 结果必须回传 | 一次调用了多个工具，必须把所有 tool message 都追加完再发起下一轮 |
| Strict 模式需切 base_url | `https://api.deepseek.com/beta`，忘记切会静默失效 |
| `insufficient_system_resource` | 需要在 orchestrator 层做重试逻辑 |

---

## 8. 与 Anthropic API 的差异速查（迁移参考）

| 维度 | DeepSeek | Anthropic Claude |
|------|----------|-----------------|
| SDK | 兼容 OpenAI SDK（`openai` 包） | 独立 `anthropic` 包 |
| Tool 字段名 | `tools[].function.parameters` | `tools[].input_schema` |
| Tool 结果角色 | `role: "tool"` | `role: "user"`, type `tool_result` |
| Tool call ID 字段 | `tool_call_id` | `tool_use_id` |
| 强制调用 | `tool_choice: "required"` | `tool_choice: {"type": "any"}` |
| Strict 模式 | Beta，需切 base_url | 原生支持 |
| 并发工具调用 | 支持（`tool_calls` 是数组） | 支持 |
