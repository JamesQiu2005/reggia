# Milkdown Crepe Load Failure — Diagnosis

> ## ✅ RESOLVED — Option A (self-host bundled), 2026-06-17
>
> The `?alias` fix below was chasing the wrong layer. By the time it was tried,
> the failure had shifted from a **code** problem (codemirror `basicSetup`
> export) to a **network** one: the browser's QUIC/HTTP3 path to esm.sh and
> jsdelivr was being dropped (`ERR_QUIC_PROTOCOL_ERROR.QUIC_NETWORK_IDLE_TIMEOUT`,
> `ERR_SOCKET_NOT_CONNECTED`), so the module never downloaded at all — even
> though `curl` (TCP) reached the same URLs with HTTP 200. No amount of esm.sh
> dependency-resolution tuning helps when the browser can't fetch from esm.sh.
>
> **Fix:** Milkdown Crepe + all deps are now bundled once with esbuild into
> `frontend/vendor/milkdown-crepe.bundle.js` (plus `milkdown-crepe.css`, KaTeX
> fonts, and the Tabler icon webfont), served locally by FastAPI's static mount.
> `codemirror` is pinned to **6.0.2** at build time via npm `overrides`, so the
> original `basicSetup` bug is structurally gone too. Build tooling lives in
> `vendor-build/` (`npm install && npm run build`). `memory.html` now does
> `import('/vendor/milkdown-crepe.bundle.js')` — zero CDN, works offline.
> Bonus: `$remark` is in the same bundle as Crepe, so the wikilink plugin is
> guaranteed one shared `@milkdown/core` instance (the old "second core" risk
> is impossible now).
>
> Everything below is kept as a record of the earlier (CDN-era) investigation.

**Date**: 2026-06-17
**Symptom**: `/memory` page shows raw markdown in a `<textarea>`, no WYSIWYG editor. Console error:
```
Milkdown failed to load, falling back to plain textarea:
SyntaxError: The requested module '/codemirror@^6.0.1?target=es2022'
does not provide an export named 'basicSetup'
```

## Root Cause

`@milkdown/crepe@7.21.2` (served from `esm.sh`) internally imports `basicSetup` from `codemirror@^6.0.1`. The `^6.0.1` semver range resolves to the **latest** 6.x version on esm.sh, which is `codemirror@6.65.7`.

`codemirror@6.65.7` no longer exports `basicSetup` as a named export — it only has a `default` export. The last version that exported `basicSetup` was **`codemirror@6.0.2`**. It was dropped after that version.

### Version timeline

| codemirror version | `basicSetup` export | Notes |
|---|---|---|
| 6.0.0, 6.0.1, **6.0.2** | ✅ present | Real CodeMirror 6 |
| 6.1.0 through 6.65.6 | N/A | These versions don't exist on npm |
| **6.65.7** | ❌ missing | Different package structure, only `default` export |

## Attempted Fixes

### Fix 1: Import map with cross-origin scope (FAILED)

Added `<script type="importmap">` with a `scopes` entry targeting `https://esm.sh/` to remap the codemirror path to v6.0.1.

**Why it failed**: The HTML spec (section 8.1.7) requires import map scope prefixes to have the **same origin** as the document. Since the document is on `localhost:8000` and modules are on `esm.sh`, the cross-origin scope was silently ignored by Chrome.

### Fix 2: esm.sh `?alias` parameter (STILL NOT WORKING)

Changed the import URL from:
```
https://esm.sh/@milkdown/crepe@7.21.2
```
to:
```
https://esm.sh/@milkdown/crepe@7.21.2?alias=codemirror:codemirror@6.0.2
```

The `?alias` parameter tells esm.sh to server-side rewrite package resolutions. In isolation, this works — esm.sh serves `crepe.mjs` with the internal codemirror import rewritten to `/codemirror@6.0.2/es2022/codemirror.mjs`. But the page still shows the textarea fallback.

**Possible reasons this still fails (not yet investigated)**:

1. **Cache**: Browser or Cloudflare cache serving the old (non-aliased) version. The `cf-cache-status: HIT` suggests Cloudflare may cache the response. The aliased URL may need a cache-bust parameter.

2. **Transitive dependency of `@milkdown/kit`**: The wikilink deps loader imports `@milkdown/kit@7.21.2/utils` (line 190) without the alias. If `@milkdown/kit`'s `utils` module transitively imports `codemirror@^6.0.1`, it would still fail.

3. **`@milkdown/kit` component imports**: When the aliased `crepe.mjs` imports from `@milkdown/kit@7.21.2/es2022/component/code-block.mjs`, that file's codemirror imports may not be affected by the alias (the alias only applies to the package named `codemirror`, but code-block.mjs may import from `@codemirror/*` scoped packages directly).

4. **esm.sh build path**: The aliased URL generates a special build path (`/X-YWNvZGVtaXJyb3I6Y29kZW1pcnJvckA2LjAuMg/`) which may have timing or CDN propagation issues.

5. **Two codemirror instances**: Even if the alias works, the aliased `codemirror@6.0.2` (providing `basicSetup`) and `@codemirror/view@^6.16.0` (imported elsewhere in crepe.mjs) may be version-incompatible, causing a different runtime error that silently falls back to the textarea.

## What Would Actually Fix This

### Option A: Self-host milkdown-crepe with frozen deps

Install `@milkdown/crepe@7.21.2` locally via npm, bundle it with all dependencies frozen at compatible versions, and serve the bundle as a static file. This removes esm.sh's runtime resolution from the equation entirely.

```bash
cd frontend
npm init -y
npm install @milkdown/crepe@7.21.2 codemirror@6.0.2
# Bundle with esbuild/rollup/vite → vendor/milkdown-crepe.bundle.mjs
```

Trade-off: Adds a JS build step to the project.

### Option B: Backend reverse proxy for esm.sh

Create a FastAPI route that proxies requests to esm.sh and rewrites `codemirror@^6.0.1` → `codemirror@6.0.2` in all `.mjs` responses. The proxy rewrites all internal imports to also flow through the proxy, giving full control over dependency resolution.

Trade-off: Adds backend complexity, slower initial loads (no CDN edge caching).

### Option C: Wait for upstream fix

Report the issue to both `@milkdown/crepe` (pin codemirror) and `esm.sh` (handle this case). Neither is under our control.

### Option D: Use UMD/IIFE build

Check if Milkdown publishes a pre-bundled UMD/IIFE version that can be loaded via a plain `<script>` tag, bypassing ESM module resolution entirely.

## Files Modified

- `frontend/memory.html` — import URL changed to include `?alias=codemirror:codemirror@6.0.2` (commit pending)
