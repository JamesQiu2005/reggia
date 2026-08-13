# Reggia

Personal chat frontend + knowledge base. Single-user, local-first.

Read agent.md to understand the code structure and what files are needed to finish the following task. Anything related to ClaudeCode as the backend include the /desktop and /chat_workspace folder is deprecated. Do not read from them

## Task

### Markdown Renderer workaround
Congrats on getting milkdown actually working ! Now the things goes down to enhancing the frontend design

1. I do not want floating editing bar since there's literally no need having that, so put formatting into the left of the editing page and make it collapsible, include everything right now in the floating formatting tool;
2. Align the font and spacing choice with Obsidian more, Always use sans serif fonts;

### memory file connection workaround

Take a look at this, the wikilink solution is never enabled:

Reference Docs/reggia-milkdown-wikilink-spec.md, start from task no.2 but do not include the fetch memory tools update yet since the local memory page is still under testing phase.

The code inside are just pseudo-code, write according the current status of the project to avoid conflict. Current status is ALWAYS the priority.

## Constraint
1. remember to do static debug first.
2. Whenever you want to open the browser to do debug let me know and I'll do the visual checking myself, this is faster
3. Keep the frontend design the same style as current Reggia.

