# Administration UIs

One Svelte 5 + TypeScript application per plugin, each talking only to its own
plugin's API. Every one is generated from `scripts/generate_ui.py` so their auth,
tenant handling and error reporting cannot drift apart.

| Plugin | UI | API port | Dev port |
|---|---|---|---|
| PAS Gateway | `ui/plugin1-ui` | 8001 | 5171 |
| IFRS 17 Valuation | `ui/plugin2-ui` | 8002 | 5172 |
| AUW Workbench | `ui/plugin3-ui` | 8003 | 5173 |
| Product Config | `ui/plugin4-ui` | 8004 | 5174 |
| Embedded Distribution | `ui/plugin5-ui` | 8005 | 5175 |
| Data Mesh | `ui/plugin6-ui` | 8006 | 5176 |
| Policy Ledger | `ui/plugin7-ui` | 8007 | 5177 |

```bash
python scripts/generate_ui.py
cd ui/plugin3-ui && npm install && npm run dev
```
