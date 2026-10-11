# LOC-Arena docs site

A static course that teaches the LOC-Arena setting to an outsider, in five chapters:

- `index.html`: the AI-control question, the two measurements, the honest twin.
- `architecture.html`: the engine every setting runs on: the gateway, the sandboxes, the services behind one MCP
  route, the sealed and observable records, and how an episode runs.
- `settings.html`, `meridian.html`, `meridian-aurora.html`: what a setting defines, the Meridian AI Lab (its
  codebase, team and permissions) and the Aurora efficiency push (the main task, the side task, a graded episode).
- `monitoring.html`: tap points, the firewall, monitor modes and timing, the aggregated suspicion score, the
  caught decision, and the safety and usefulness metrics.
- `extend.html`: setup, running and reading episodes, and configuring or extending tasks, monitors and models.

Plus `style.css` (shared, light and dark readable, phone friendly) and inline SVG figures.

## How it is served

Plain static HTML, no build step. Point GitHub Pages at this folder: in the repository settings, choose the branch
and the site folder, and Pages serves these files as-is.
