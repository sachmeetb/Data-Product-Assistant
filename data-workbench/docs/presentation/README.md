# Data Workbench presentations

## Multi-platform onboarding architecture

[`multi-platform-onboarding.html`](multi-platform-onboarding.html) is a standalone, offline architecture-committee deck covering the proposed adapter architecture, dialect controls, deployment patterns, technology and license selections, conformance strategy, and rollout plan.

It has no external runtime dependencies. Open the file directly in a browser. Use `Left` / `Right` to navigate, `O` for the slide overview, `N` for speaker notes, `F` for fullscreen, `?` for help, and `P` to print or save as PDF.

## General Data Workbench overview

A self-contained reveal.js deck explaining what the Data Workbench is, how it works for product owners and engineers, and how the data-transformation system is structured.

## Open it

```bash
# from anywhere on this machine
xdg-open docs/presentation/index.html   # Linux
open    docs/presentation/index.html   # macOS
start   docs/presentation\index.html   # Windows
```

The deck loads reveal.js + theme + highlight from a CDN, so the browser needs an internet connection on first open.

## Navigate

| Key       | What it does                              |
| --------- | ----------------------------------------- |
| `→` / `←` | Next / previous slide                     |
| `↓` / `↑` | Sub-slides (none in this deck currently)  |
| `S`       | Open speaker view in a second window      |
| `F`       | Fullscreen                                |
| `ESC`     | Slide overview                            |
| `?`       | Keyboard shortcut cheatsheet              |

## Export to PDF

Append `?print-pdf` to the URL and print to PDF from the browser:

```
file:///path/to/docs/presentation/index.html?print-pdf
```

Then `Ctrl+P` → Save as PDF. Set "Background graphics" on for the dark theme to render correctly.

## Structure

| Section                       | Slides | Coverage |
| ----------------------------- | ------ | -------- |
| 01. The paradigm              | 1–7    | Problem, solution, two personas, architecture, KG standards |
| 02. How it works              | 8–16   | Archetypes, workflows, SA/CF walkthroughs, reviews, DQ rules, scoring, marketplace |
| 03. Data transformations      | 17–25  | Column kinds, deep-dives, dataset Shape, SCDs, multi-dialect |
| 04. Capabilities              | 26–30  | Chat assistants, Apply protocol, active learning, CLI execution |
| 05. Wrap-up                   | 31–33  | Quick start, Q&A                                              |
| Appendix                      | 34–37  | Auto-bridge, PROV-O, versioned contracts, skill registry      |

## Editing

`index.html` is a single hand-authored file. Inline SVG for diagrams (no external image dependencies beyond `images/`). To change colors, edit the `:root` custom properties at the top of the `<style>` block; the two persona accents are `--color-po` (violet) and `--color-eng` (blue).

External assets live in `images/`:
- `ui-marketplace.png`, `ui-pipeline.png` — copied from repo `screenshots/`
- `01-layered-architecture.svg`, `02-deployment-topology.svg` — copied from `../../research/techarch-assets/` for optional architecture-tab use

## Source material referenced

- `../../README.md` — feature inventory
- `../userguide.md` — operator narratives
- `../architecture.md` — deeper architectural reference
- `../../CLAUDE.md` — decision log (DQ taxonomy, transformation kinds, Shape, SCDs, multi-dialect)
- `../../research/demo_script.md` — informed slide sequencing
