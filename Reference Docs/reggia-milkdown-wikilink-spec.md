# Reggia Memory Editor — Milkdown Integration & Wikilink Spec

## 1. Editor Setup: Milkdown Crepe

### Installation

```bash

```

### Initialization (vanilla JS)

```javascript
import { crepe } from '@milkdown/crepe';
import '@milkdown/crepe/theme/common/style.css';
import '@milkdown/crepe/theme/frame.css'; // or 'classic' — test both, pick what fits Reggia's visual language

const editor = await crepe({
  root: document.getElementById('editor-container'),
  defaultValue: '',  // populated from GET /api/memory/files/:path
  features: {
    'code-block': true,
    'list-item': true,
    'link-tooltip': true,
    'image-block': false,   // not needed for memory files
    'block-edit': false,    // keep it simple
    'placeholder': true,
    'toolbar': false,       // no floating toolbar — keep it clean like Obsidian
  },
});
```

### Auto-save

```javascript
import { listenerCtx } from '@milkdown/plugin-listener';

let saveTimer = null;

// Listen for doc changes
editor.action((ctx) => {
  ctx.get(listenerCtx).markdownUpdated((ctx, markdown, prevMarkdown) => {
    if (markdown === prevMarkdown) return;

    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      await fetch(`/api/memory/files/${encodeURIComponent(currentFilePath)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: markdown }),
      });
      showSaveIndicator();  // subtle "Saved" text, fade out after 1.5s
    }, 2000);  // 2s debounce
  });
});
```

### Loading a Different File

When user clicks a file card in the right sidebar:

```javascript
async function openFile(filePath) {
  // Flush any pending save for current file
  if (saveTimer) {
    clearTimeout(saveTimer);
    await saveCurrentFile();
  }

  const res = await fetch(`/api/memory/files/${encodeURIComponent(filePath)}`);
  const data = await res.json();

  editor.setMarkdown(data.content);
  currentFilePath = filePath;
  highlightActiveCard(filePath);
}
```

---

## 2. Wikilink Support

### Scope

Support these Obsidian wikilink forms:

| Syntax | Behavior | Day-one support |
|--------|----------|-----------------|
| `[[filename]]` | Link to filename.md | Yes |
| `[[filename\|display text]]` | Link with custom label | Yes |
| `[[folder/filename]]` | Link with path | Yes |
| `[[filename#heading]]` | Link to heading in file | Parse & store, render as file link (no scroll-to-heading) |
| `[[filename#heading\|text]]` | Heading link with label | Same as above |
| `![[filename]]` | Embed file content | Parse & store, render as regular link (no inline embed) |
| `[[#heading]]` | Link within current file | Ignore in link graph, render as plain text |
| `[[filename#^block-id]]` | Block reference | Ignore |

Day-one principle: **parse everything, render the basics, store all links in the graph.** Heading anchors and embeds are recognized and preserved in the markdown (never stripped or corrupted), but the editor renders them as simple clickable links.

### Milkdown Wikilink Plugin

Milkdown doesn't have a built-in wikilink plugin. Write a custom remark plugin that integrates with Milkdown's remark pipeline.

```javascript
// wikilink-plugin.js
// Remark plugin to parse [[wikilinks]] into mdast nodes

import { findAndReplace } from 'mdast-util-find-and-replace';

const WIKILINK_REGEX = /!?\[\[([^\]]+?)\]\]/g;

function remarkWikilink() {
  return (tree) => {
    findAndReplace(tree, [
      [WIKILINK_REGEX, (match, inner) => {
        const isEmbed = match.startsWith('!');
        let target = inner;
        let displayText = null;
        let heading = null;

        // Split display text: [[target|display]]
        const pipeIndex = inner.indexOf('|');
        if (pipeIndex !== -1) {
          target = inner.slice(0, pipeIndex);
          displayText = inner.slice(pipeIndex + 1);
        }

        // Split heading: [[target#heading]]
        const hashIndex = target.indexOf('#');
        if (hashIndex !== -1) {
          heading = target.slice(hashIndex + 1);
          target = target.slice(0, hashIndex);
        }

        // Render as a clickable link node
        return {
          type: 'link',
          url: `wikilink://${target}${heading ? '#' + heading : ''}`,
          children: [{ type: 'text', value: displayText || inner }],
          data: {
            hProperties: {
              className: isEmbed ? 'wikilink wikilink-embed' : 'wikilink',
              'data-target': target,
              'data-heading': heading || '',
              'data-embed': isEmbed,
            }
          }
        };
      }]
    ]);
  };
}
```

Register with Milkdown:

```javascript
import { $remark } from '@milkdown/kit/utils';

const wikilinkPlugin = $remark('wikilink', () => remarkWikilink);

// Add to editor setup
const editor = await crepe({
  root: document.getElementById('editor-container'),
  defaultValue: '',
  // ...
});
editor.use(wikilinkPlugin);
```

### Wikilink Click Handling

```javascript
document.getElementById('editor-container').addEventListener('click', (e) => {
  const link = e.target.closest('a.wikilink');
  if (!link) return;
  e.preventDefault();

  const target = link.dataset.target;
  if (!target) return;

  // Resolve target to a file path
  const resolvedPath = resolveWikilink(target);
  if (resolvedPath) {
    openFile(resolvedPath);
  }
});

function resolveWikilink(target) {
  // Search all known files for a match
  // Priority: exact path match > filename match in any folder
  const allFiles = getFileList();  // cached from last /api/memory/files call

  // 1. Exact path match (with or without .md)
  const withExt = target.endsWith('.md') ? target : target + '.md';
  if (allFiles.includes(withExt)) return withExt;

  // 2. Filename match anywhere in vault
  const filename = withExt.split('/').pop();
  const match = allFiles.find(f => f.endsWith('/' + filename) || f === filename);
  return match || null;
}
```

### Wikilink Styling

```css
a.wikilink {
  color: var(--reggia-accent);
  text-decoration: none;
  border-bottom: 1px solid var(--reggia-accent-muted);
  cursor: pointer;
}

a.wikilink:hover {
  border-bottom-color: var(--reggia-accent);
}

/* Unresolved links — target file doesn't exist */
a.wikilink.unresolved {
  color: var(--reggia-text-muted);
  border-bottom-style: dashed;
}

/* Embed links — visual distinction but no inline rendering yet */
a.wikilink.wikilink-embed::before {
  content: '⊞ ';
  font-size: 0.8em;
  opacity: 0.5;
}
```

### Preserving Raw Wikilink Syntax

Critical: when the editor serializes back to markdown (for auto-save), wikilinks must be written back as `[[...]]` syntax, not converted to standard markdown links. This is handled by a custom remark-stringify extension:

```javascript
function remarkWikilinkStringify() {
  return (tree) => {
    // Visit all link nodes, convert wikilink:// URLs back to [[]] syntax
    visit(tree, 'link', (node) => {
      if (node.url && node.url.startsWith('wikilink://')) {
        const target = node.url.replace('wikilink://', '');
        const displayText = node.children?.[0]?.value;
        const isEmbed = node.data?.hProperties?.['data-embed'] === true;

        const prefix = isEmbed ? '!' : '';

        if (displayText && displayText !== target) {
          node.type = 'text';
          node.value = `${prefix}[[${target}|${displayText}]]`;
          delete node.children;
          delete node.url;
        } else {
          node.type = 'text';
          node.value = `${prefix}[[${target}]]`;
          delete node.children;
          delete node.url;
        }
      }
    });
  };
}
```

---

## 3. Backend: Link Graph Extraction

### On File Save

Every time a file is saved via `PUT /api/memory/files/:path`, the backend parses wikilinks and updates a link index.

```python
import re
from pathlib import Path

WIKILINK_PATTERN = re.compile(r'!?\[\[([^\]|#]+)(?:#[^\]|]*)?\|?[^\]]*\]\]')

def extract_links(markdown_content: str) -> list[str]:
    """Extract unique link targets from markdown content.
    Returns filenames/paths without .md extension, without heading fragments."""
    targets = set()
    for match in WIKILINK_PATTERN.finditer(markdown_content):
        target = match.group(1).strip()
        if target:
            targets.add(target)
    return sorted(targets)

def resolve_link(target: str, vault_path: Path) -> str | None:
    """Resolve a wikilink target to an actual file path relative to vault root."""
    # Exact path match
    candidate = vault_path / (target if target.endswith('.md') else target + '.md')
    if candidate.exists():
        return str(candidate.relative_to(vault_path))

    # Filename search across vault
    filename = target.split('/')[-1]
    if not filename.endswith('.md'):
        filename += '.md'
    for f in vault_path.rglob(filename):
        return str(f.relative_to(vault_path))

    return None  # unresolved link
```

### Link Index Storage

Store link graph in a simple JSON file at vault root (`_links.json`), regenerated on every file save. Not a new database — just a cache file.

```json
{
  "skills.md": {
    "links_to": ["projects/jarvis", "preferences"],
    "linked_from": ["index.md"]
  },
  "projects/jarvis.md": {
    "links_to": [],
    "linked_from": ["skills.md", "projects/reggia.md"]
  }
}
```

Rebuild logic:

```python
def rebuild_link_index(vault_path: Path) -> dict:
    index = {}
    all_files = [str(f.relative_to(vault_path)) for f in vault_path.rglob('*.md')]

    # Forward pass: extract outgoing links
    for filepath in all_files:
        content = (vault_path / filepath).read_text()
        links = extract_links(content)
        index[filepath] = {"links_to": links, "linked_from": []}

    # Backward pass: compute incoming links
    for filepath, data in index.items():
        for target in data["links_to"]:
            resolved = resolve_link(target, vault_path)
            if resolved and resolved in index:
                index[resolved]["linked_from"].append(
                    filepath.replace('.md', '')
                )

    return index
```

### API Extension

```
GET /api/memory/links              → returns full _links.json
GET /api/memory/links/:path        → returns link data for one file
```

---

## 4. fetch_memory Tool Update

### Response Format

When the agent calls `fetch_memory(page="skills.md")`, the response now includes a `links` field:

```json
{
  "content": "# Skills\n\nHanze's technical skills...\n\nSee [[projects/jarvis]] for...",
  "links": {
    "links_to": ["projects/jarvis", "preferences"],
    "linked_from": ["index.md"]
  }
}
```

### `<ctx/>` Tag Extension

When storing the tool call metadata and constructing the `<ctx/>` tag:

```
<ctx src="skills.md" links_to="projects/jarvis, preferences" linked_from="index.md"/>
```

### System Prompt Addition

Add to the existing context fetch dedup rule:

```
## Link-aware Memory Navigation

<ctx/> 标签可能包含 links_to 和 linked_from 属性，表示该 memory page 与其他 page 的引用关系。

- links_to: 该 page 内容中引用了哪些其他 page
- linked_from: 哪些其他 page 引用了该 page

当用户的问题可能需要被引用 page 的详细内容时，可以根据 links_to 主动 fetch 相关 page。
但优先判断当前已有的 context 是否足够回答，避免不必要的 fetch。

不要仅因为 link 存在就 fetch——只在回答需要被引用 page 的具体内容时才 follow link。
```

---

## 5. Unresolved Link Handling

### In Editor

When rendering wikilinks, check against the cached file list. Unresolved links get the `.unresolved` CSS class (dashed underline, muted color). Clicking an unresolved link prompts: "Create [filename].md?" — if confirmed, creates the file and opens it.

### In fetch_memory

If a page contains wikilinks to files that don't exist, include them in a separate field:

```json
{
  "content": "...",
  "links": {
    "links_to": ["projects/jarvis"],
    "unresolved": ["ideas/someday"],
    "linked_from": ["index.md"]
  }
}
```

Model should ignore unresolved links and not attempt to fetch them.

---

## 6. File Compatibility Rules

To maintain Obsidian vault compatibility:

- **File encoding:** UTF-8, no BOM
- **Line endings:** LF (Unix-style)
- **No metadata injection:** Don't add frontmatter, comments, or hidden markers to files. They are the user's documents.
- **`_links.json` is a cache:** It can be deleted and regenerated. Obsidian will ignore it. Prefix with `_` to signal it's not a user document.
- **`.obsidian/` folder:** If the user has pointed Obsidian at this vault, there will be a `.obsidian/` config folder. Ignore it entirely — don't read, write, delete, or list its contents.
- **File naming:** Support any UTF-8 filename that Obsidian supports. Avoid only: `\ / : * ? " < > |`

---

## 7. Implementation Order

1. Install Milkdown, replace current editor area with crepe instance
2. Wire up auto-save to existing PUT endpoint
3. Add wikilink remark plugin (parse + render as styled links)
4. Add wikilink stringify plugin (preserve `[[]]` on save)
5. Add click-to-navigate for wikilinks
6. Backend: add `extract_links()` and `_links.json` rebuild on file save
7. Backend: add `GET /api/memory/links` endpoint
8. Update `fetch_memory` tool response to include `links` field
9. Update `<ctx/>` tag format and system prompt rule
10. Add unresolved link styling + "Create file?" prompt
