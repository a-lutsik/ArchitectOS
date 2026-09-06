# Agent notes

Cursor project rules live in `.cursor/rules/` and apply to every model in this repo.

## Mermaid

Architecture, call-flow, layers, and DB diagrams must use straight/orthogonal edges.

Start every such fence with:

```
---
config:
  layout: elk
  flowchart:
    curve: linear
    nodeSpacing: 40
    rankSpacing: 50
---
flowchart TB
```

Do not use `curve: basis` (or `cardinal` / `natural` / `look: handDrawn`) on those diagrams. Smooth curves are allowed only on UX/onboarding journeys, and only on the specific edges that need them (`e1@{ curve: basis }`).

Ask in ArchitectOS injects the same policy via `RESPONSE_FORMAT_POLICY` and renders flowcharts with `curve: linear`.
