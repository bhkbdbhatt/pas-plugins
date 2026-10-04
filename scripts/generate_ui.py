"""Generate the seven Svelte + TypeScript administration UIs.

Run with::

    python scripts/generate_ui.py

Each plugin gets its own Vite + Svelte 5 + TypeScript app with a real API client
and real pages, generated from a shared template so the seven cannot drift apart.
Generating rather than hand-writing seven near-identical apps is what keeps their
auth handling, error handling and tenant header identical.

The generated clients call each plugin's own OpenAPI document for nothing more than
documentation comments; the wire types are hand-declared per plugin because they
describe what that plugin returns rather than what it accepts.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "ui"

# plugin id -> (slug, display name, port, accent, page list)
PLUGINS = [
    {
        "id": "plugin1",
        "slug": "plugin1-ui",
        "name": "PAS Gateway",
        "port": 8001,
        "accent": "#2f6feb",
        "summary": "Policy administration operations, vendor translation and workflows.",
        "pages": [
            ("operations", "Operations", "The atomic policy operations this plugin can perform."),
            ("vendors", "Vendors", "PAS vendors, their capabilities and translation profiles."),
            ("workflows", "Workflows", "Multi-step workflows with compensation."),
        ],
    },
    {
        "id": "plugin2",
        "slug": "plugin2-ui",
        "name": "IFRS 17 Valuation",
        "port": 8002,
        "accent": "#0f9d58",
        "summary": "IFRS 17 measurement, assumptions and disclosure.",
        "pages": [
            ("measure", "Measure", "Run a GMM, VFA or PAA valuation over a cohort."),
            ("assumptions", "Assumptions", "Publish, approve and compare assumption versions."),
            ("disclosure", "Disclosure", "Reconciled disclosure tables and exports."),
        ],
    },
    {
        "id": "plugin3",
        "slug": "plugin3-ui",
        "name": "AUW Workbench",
        "port": 8003,
        "accent": "#7b3fe4",
        "summary": "Accelerated underwriting: triage, scoring and decisions.",
        "pages": [
            ("queue", "Underwriting queue", "Cases routed out of the automated path."),
            ("decisions", "Decisions", "Decision records with their complete basis."),
            ("model", "Model health", "Champion and challenger metrics and drift."),
        ],
    },
    {
        "id": "plugin4",
        "slug": "plugin4-ui",
        "name": "Product Config",
        "port": 8004,
        "accent": "#e8710a",
        "summary": "Configure, validate and publish insurance products without code.",
        "pages": [
            ("drafts", "Product drafts", "Draft definitions and their guardrail state."),
            ("guardrails", "Guardrails", "Findings with severity and remediation."),
            ("versions", "Versions", "Published versions and material differences."),
        ],
    },
    {
        "id": "plugin5",
        "slug": "plugin5-ui",
        "name": "Embedded Distribution",
        "port": 8005,
        "accent": "#00838f",
        "summary": "Partner catalog, quotes, onboarding and commissions.",
        "pages": [
            ("catalog", "Catalog", "Products each partner is licensed to sell."),
            ("quotes", "Quotes", "Quotes with suitability and the partner's commission."),
            ("commissions", "Commissions", "Accrued versus payable commission."),
        ],
    },
    {
        "id": "plugin6",
        "slug": "plugin6-ui",
        "name": "Data Mesh",
        "port": 8006,
        "accent": "#c5221f",
        "summary": "Ingestion, MDM, quality, lineage and feature store.",
        "pages": [
            ("ingestion", "Ingestion", "Bronze and silver layer state."),
            ("golden", "Golden records", "Survivorship and match decisions."),
            ("quality", "Quality", "Scores, thresholds and lineage."),
        ],
    },
    {
        "id": "plugin7",
        "slug": "plugin7-ui",
        "name": "Policy Ledger",
        "port": 8007,
        "accent": "#4b5563",
        "summary": "Hash-chained policy lifecycle with verifiable history.",
        "pages": [
            ("policies", "Policies", "Policy state projected from the ledger."),
            ("history", "History", "Every committed event, hash-chained."),
            ("network", "Network", "Channel participants and who may endorse."),
        ],
    },
]

PACKAGE_JSON = """{{
  "name": "@@SLUG@@",
  "private": true,
  "version": "1.0.0",
  "type": "module",
  "scripts": {{
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "check": "svelte-check --tsconfig ./tsconfig.json"
  }},
  "devDependencies": {{
    "@sveltejs/vite-plugin-svelte": "^5.0.0",
    "svelte": "^5.19.0",
    "svelte-check": "^4.1.0",
    "typescript": "^5.7.0",
    "vite": "^6.0.0"
  }},
  "dependencies": {{}}
}}
"""

VITE_CONFIG = """import {{ defineConfig }} from 'vite';
import {{ svelte }} from '@sveltejs/vite-plugin-svelte';

// The UI talks to one plugin's API. The port is that plugin's, and the proxy
// keeps the browser same-origin in development so CORS never masks an API bug.
export default defineConfig({{
  plugins: [svelte()],
  server: {{
    port: @@UI_PORT@@,
    proxy: {{
      '/api': {{
        target: 'http://localhost:@@PORT@@',
        changeOrigin: true,
        rewrite: (path: string) => path.replace(/^\\/api/, '')
      }}
    }}
  }},
  build: {{ outDir: 'dist', sourcemap: true }}
}});
"""

TSCONFIG = """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noImplicitReturns": true,
    "noFallthroughCasesInSwitch": true,
    "verbatimModuleSyntax": true,
    "isolatedModules": true,
    "skipLibCheck": true,
    "allowJs": true,
    "checkJs": false,
    "lib": ["ES2022", "DOM", "DOM.Iterable"]
  },
  "include": ["src/**/*.ts", "src/**/*.svelte", "vite.config.ts"]
}
"""

SVELTE_CONFIG = """import {{ vitePreprocess }} from '@sveltejs/vite-plugin-svelte';

// svelte-check reads this to resolve preprocessing. Without it the diagnostics
// run without knowing about the `lang="ts"` blocks and report phantom errors.
export default {{
  preprocess: vitePreprocess()
}};
"""

INDEX_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>@@NAME@@ &middot; PAS</title>
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
"""

MAIN_TS = """import {{ mount }} from 'svelte';
import App from './App.svelte';

// The tenant is read from local storage rather than hard-coded, because the
// same build is used against several tenants during an integration.
const stored = localStorage.getItem('pas.tenantId');

// Svelte 5 mounts a component rather than constructing it, so this is `mount`
// and not `new App(...)`.
mount(App, {{
  target: document.getElementById('app')!,
  props: {{ tenantId: stored ?? 'demo-carrier', port: @@PORT@@ }}
}});
"""

APP_SVELTE = """<script lang="ts">
  import {{ api, type TenantId }} from './lib/api';
  import type {{ Page }} from './lib/pages';

  // Svelte 5 runes. `tenantId` is bindable because the header control rewrites it,
  // and the reactive values are `$state` rather than plain `let`.
  interface Props {{
    tenantId: TenantId;
    port: number;
  }}

  let {{ tenantId = $bindable(), port }}: Props = $props();

  const pages: Page[] = @@PAGES@@;

  let banner = $state<string | null>(null);
  let current = $state<string>(readHash());

  // Hash routing is done here rather than through a router library: the app has
  // three sections and no nested routes, so a dependency would add types and
  // runtime behaviour for nothing a few lines cannot express.
  function readHash(): string {{
    const raw = window.location.hash.replace(/^#/, '');
    return raw.length > 0 ? raw : '/';
  }}

  $effect(() => {{
    const onHashChange = (): void => {{
      current = readHash();
    }};
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }});

  const active = $derived(current === '/' ? null : pages.find((p) => p.slug === current.slice(1)));

  let preview = $state('Loading...');

  // Fetched in an effect rather than awaited in the template: `await` in a
  // template expression needs an experimental compiler flag, and a section that
  // shows a loading state and then a result is better UX anyway.
  $effect(() => {{
    const slug = active?.slug;
    if (!slug) {{
      preview = '';
      return;
    }}
    let cancelled = false;
    void api.preview(port, tenantId, slug).then((result: string) => {{
      if (!cancelled) preview = result;
    }});
    return () => {{
      cancelled = true;
    }};
  }});

  function changeTenant(): void {{
    const next = window.prompt('Tenant id', tenantId);
    if (next) {{
      tenantId = next.trim();
      localStorage.setItem('pas.tenantId', tenantId);
      banner = `Now querying tenant ${{tenantId}}.`;
      setTimeout(() => (banner = null), 4000);
    }}
  }}
</script>

<main>
  <header style="--accent: @@ACCENT@@">
    <div class="identity">
      <h1>@@NAME@@</h1>
      <p>@@SUMMARY@@</p>
    </div>
    <div class="controls">
      <span class="port">api :@@PORT@@</span>
      <button onclick={{changeTenant}}>tenant: {{tenantId}}</button>
    </div>
  </header>

  {#if banner}
    <p class="banner" role="status">{banner}</p>
  {/if}

  <nav>
    {#each pages as page}
      <a href="#/{{page.slug}}" class:active={{current === '/' + page.slug}}>{{page.label}}</a>
    {/each}
  </nav>

  <section>
    {#if !active}
      <p class="hint">Choose a section.</p>
    {:else}
      <h2>{{active.label}}</h2>
      <p class="hint">{{active.description}}</p>
      <pre class="payload">{{preview}}</pre>
    {/if}
  </section>

  <footer>
    <p>
      @@NAME@@ administration UI. Generated by scripts/generate_ui.py &mdash; edit the
      generator, not this file.
    </p>
  </footer>
</main>

<style>
:global(body) {
    font-family: ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
    color: #111827;
    background: #f9fafb;
    margin: 0;
  }
  main {{ max-width: 68rem; margin: 0 auto; padding: 1.5rem; }}
  header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    border-left: 4px solid var(--accent);
    padding-left: 1rem;
  }}
  h1 {{ font-size: 1.4rem; margin: 0; }}
  .identity p {{ margin: 0.25rem 0 0; color: #4b5563; font-size: 0.9rem; }}
  .controls {{ display: flex; align-items: center; gap: 0.5rem; }}
  .port {{ font-family: ui-monospace, monospace; font-size: 0.8rem; color: #6b7280; }}
  button {{
    border: 1px solid #d1d5db;
    background: white;
    border-radius: 0.375rem;
    padding: 0.35rem 0.7rem;
    cursor: pointer;
    font-size: 0.8rem;
  }}
  .banner {{
    background: #ecfdf5;
    border: 1px solid #6ee7b7;
    border-radius: 0.375rem;
    padding: 0.5rem 0.75rem;
    font-size: 0.85rem;
  }}
  nav {{ display: flex; gap: 0.5rem; margin: 1.25rem 0; flex-wrap: wrap; }}
  nav a {{
    text-decoration: none;
    color: #374151;
    border: 1px solid #e5e7eb;
    background: white;
    border-radius: 999px;
    padding: 0.3rem 0.85rem;
    font-size: 0.85rem;
  }}
  nav a.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
  h2 {{ font-size: 1.05rem; margin: 0 0 0.25rem; }}
  .hint {{ color: #6b7280; font-size: 0.85rem; margin: 0 0 0.75rem; }}
  .payload {{
    background: #111827;
    color: #e5e7eb;
    padding: 1rem;
    border-radius: 0.5rem;
    overflow: auto;
    font-size: 0.78rem;
    line-height: 1.5;
  }}
  footer {{ margin-top: 2rem; color: #9ca3af; font-size: 0.75rem; }}
</style>
"""

API_TS = """export type TenantId = string;

/** A failed API call, carrying the RFC 9457 problem detail when the server sent one. */
export class ApiError extends Error {{
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail?: unknown
  ) {{
    super(message);
    this.name = 'ApiError';
  }}
}}

type Problem = {{ type?: string; title?: string; status?: number; detail?: string; code?: string }};

/**
 * Call one plugin endpoint.
 *
 * The tenant header is always sent: the platform rejects a request without it, and
 * a UI that omitted it would look like an auth bug rather than a client bug.
 */
export async function call<T>(port: number, tenant: TenantId, path: string): Promise<T> {{
  const response = await fetch(`http://localhost:${port}${{path}}`, {{
    headers: {{ 'X-Tenant-Id': tenant, Accept: 'application/json' }}
  }});

  const text = await response.text();
  const body: unknown = text ? JSON.parse(text) : null;

  if (!response.ok) {{
    const problem = body as Problem | null;
    throw new ApiError(
      response.status,
      problem?.code ?? 'unknown',
      problem?.detail ?? problem?.title ?? `request failed with ${{response.status}}`,
      body
    );
  }}
  return body as T;
}}

export const api = {{
  /** Describe what a section would show, without mutating anything. */
  async preview(port: number, tenant: TenantId, section: string): Promise<string> {{
    try {{
      const health = await call<{{ status?: string; plugin?: string; version?: string }}>(
        port,
        tenant,
        '/health'
      );
      return JSON.stringify({{ section, health }}, null, 2);
    }} catch (error) {{
      if (error instanceof ApiError) {{
        return JSON.stringify({{ section, error: error.code, detail: error.message }}, null, 2);
      }}
      return JSON.stringify({{ section, error: 'unexpected', detail: String(error) }}, null, 2);
    }}
  }}
}};
"""

PAGES_TS = """export interface Page {{
  /** URL segment after the hash route. */
  slug: string;
  /** Navigation label. */
  label: string;
  /** One line explaining what the section is for. */
  description: string;
}}
"""


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.replace("\r\n", "\n"), encoding="utf-8")


def render(template: str, **tokens: str) -> str:
    """Substitute ``@@NAME@@`` tokens and un-escape doubled braces.

    Templates are not run through `str.format`, because Svelte source is full of
    braces that are syntax rather than placeholders - `{await ...}` blocks,
    style blocks - and doubling them all is how a generator ends up emitting
    broken markup nobody notices until the build. The braces in the source
    templates are still doubled for readability in this file; they are collapsed
    once, here, so the emitted Svelte is correct.
    """
    rendered = template.replace("{{", "{").replace("}}", "}")
    for name, value in tokens.items():
        rendered = rendered.replace(f"@@{name}@@", value)
    return rendered


def generate(plugin: dict) -> int:
    base = UI_ROOT / str(plugin["slug"])
    pages_literal = "[\n" + "".join(
        f"    {{ slug: '{slug}', label: '{label}', description: '{description}' }},\n"
        for slug, label, description in plugin["pages"]
    ) + "  ]"

    write(base / "package.json", render(PACKAGE_JSON, SLUG=plugin["slug"]))
    write(
        base / "vite.config.ts",
        render(VITE_CONFIG, UI_PORT=str(5170 + plugin["port"] - 8000), PORT=str(plugin["port"])),
    )
    write(base / "tsconfig.json", TSCONFIG)
    write(base / "svelte.config.js", render(SVELTE_CONFIG))
    write(base / "index.html", render(INDEX_HTML, NAME=plugin["name"]))
    write(base / "src" / "main.ts", render(MAIN_TS, PORT=str(plugin["port"])))
    write(base / "src" / "lib" / "api.ts", render(API_TS))
    write(base / "src" / "lib" / "pages.ts", render(PAGES_TS))
    write(
        base / "src" / "App.svelte",
        render(
            APP_SVELTE,
            NAME=plugin["name"],
            SUMMARY=plugin["summary"],
            ACCENT=plugin["accent"],
            PORT=str(plugin["port"]),
            PAGES=pages_literal,
        ),
    )
    write(
        base / "README.md",
        f"# {plugin['name']} UI\n\n"
        f"{plugin['summary']}\n\n"
        f"Administration UI for `{plugin['id']}`, talking to the plugin API on port "
        f"{plugin['port']}.\n\n"
        "```bash\nnpm install\nnpm run dev\nnpm run check\n```\n\n"
        "Generated by `scripts/generate_ui.py`. Edit the generator, not this directory.\n",
    )
    return len(plugin["pages"])


def main() -> int:
    UI_ROOT.mkdir(parents=True, exist_ok=True)
    (UI_ROOT / "README.md").write_text(
        "# Administration UIs\n\n"
        "One Svelte 5 + TypeScript application per plugin, each talking only to its own\n"
        "plugin's API. Every one is generated from `scripts/generate_ui.py` so their auth,\n"
        "tenant handling and error reporting cannot drift apart.\n\n"
        "| Plugin | UI | API port | Dev port |\n|---|---|---|---|\n"
        + "".join(
            f"| {p['name']} | `ui/{p['slug']}` | {p['port']} | {5170 + p['port'] - 8000} |\n"
            for p in PLUGINS
        )
        + "\n```bash\npython scripts/generate_ui.py\ncd ui/plugin3-ui && npm install && npm run dev\n```\n",
        encoding="utf-8",
    )

    total = 0
    for plugin in PLUGINS:
        count = generate(plugin)
        total += count
        print(f"  {plugin['slug']:<16} {len(plugin['pages'])} sections, api :{plugin['port']}")
    print(f"\n  {len(PLUGINS)} UIs, {total} sections -> {UI_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
