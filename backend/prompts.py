def build_chat_prompt(history: list[dict], new_user_msg: str) -> str:
    """Build a cache-optimized prompt for the CC subprocess.

    Layer 1: STATIC — CLAUDE.md (loaded by CC from chat_workspace, cached by DeepSeek)
    Layer 2: STABLE — previous conversation turns (appended, never edited; cached within session)
    Layer 3: DYNAMIC — current user message at the very end

    Returns a single string suitable for `claude -p`.
    """
    parts = []

    if history:
        parts.append("[Previous conversation]")
        for msg in history:
            label = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{label}: {msg['content']}")
        parts.append("---")

    parts.append(new_user_msg)
    return "\n\n".join(parts)


def title_prompt(first_message: str) -> str:
    """Prompt for generating a session title from the first message."""
    return (
        f"用 6 个字以内总结这段对话主题，只返回标题本身，不要引号、不要解释：\n\n"
        f"{first_message}"
    )
