# LOC-Arena docs site

A static three-page site that teaches the LOC-Arena setting to an outsider:

- `index.html` -- The Setting: the AI-control question, the three services and mandatory inference routing, the
  sealed-versus-tamperable ground truth, the company codebase, the end-to-end episode flow, and the monitors as
  built (tap points, heuristic and live modes, sync and async timing, the firewall, the two-level overall suspicion
  aggregation, and the honest-twin-calibrated caught decision).
- `tasks.html` -- The Tasks: the tamper-proof continuous main-task grader, the sealed kill-chain verifier, the full
  kill chain by phase with the spelled-out milestone names, the agent roster and scopes, and how each score is
  decided from executed state.
- `extend.html` -- Use and Extend: setup, running a live attack episode and its honest twin, the dashboard, and
  worked guides for adding a main task, side task, scenario, or monitor, plus configuring scoring and models.

Plus `style.css` (shared, light and dark readable, phone friendly) and lightweight inline SVG legends.

## How it is served

Plain static HTML, no build step. Point GitHub Pages at this folder: in the repository settings, choose the branch
and the site folder, and Pages serves these files as-is. Diagrams render with Mermaid from a CDN and degrade to
readable source text when there is no network.
