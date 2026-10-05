# MCP Raster Image Prompts

Use these prompts as the source for AI-generated raster versions of the MCP
architecture diagrams. Attach a style reference image when generating, then save the
selected outputs back into `docs/diagrams/` with non-colliding names such as:

- `mcp_unified_endpoint_ai_v1.png`
- `mcp_two_front_doors_ai_v1.png`
- `mcp_request_lifecycle_ai_v1.png`

## Global Style Instruction

```text
Use the attached image as a STYLE REFERENCE only.

Match its polished presentation-infographic quality: rounded translucent panels,
soft blue technical background, subtle circuit/data-flow lines, colorful status dots,
directional arrows, small 3D/vector-style icons, and a clean enterprise architecture
feel.

Do not copy the exact content of the reference image. Create a new diagram about the
Data Workbench MCP endpoint.

Important:
- 16:9 wide presentation slide.
- Light background, not dark.
- Richer and more dimensional than a code-generated SVG.
- Keep text short and legible.
- Prefer icons, badges, status dots, and visual grouping over long paragraphs.
- Avoid random filler text, watermarks, logos, cartoon characters, or clutter.
```

## Diagram 1 - Unified MCP Endpoint

```text
Create a polished 16:9 architecture infographic titled:

"DATA WORKBENCH MCP ENDPOINT"

Visual story:
A central glowing service node labeled "/mcp" and "Data Workbench MCP" sits in the
middle.

Left side:
Three client cards feed into the MCP node:
- Claude Code
- IDE agent
- OpenAI Codex

Also show a small client-side guide card labeled:
"workbench-guide"

Center:
The MCP node should feel like a secure gateway/control plane. Include small badges:
- bearer token
- project scoped
- 22 tools

Right side:
Show five grouped tool lanes/cards:
- Read
- Mechanical
- Interactive
- Review-write
- Agentic

The Agentic lane should route through an amber module labeled:
"Agent Harness"

Bottom:
A wide green band labeled:
"Server-side Skills Pool"

Show many small skill capsules/icons inside the band, but do not use tiny unreadable
names. The key idea is that agentic tools load server-side skills.

Style:
Use a sophisticated SaaS architecture look, rounded glass panels, soft shadows,
subtle 3D icons, blue/green/amber/purple accents, and clean directional arrows.
```

## Diagram 2 - Two Front Doors

```text
Create a polished 16:9 architecture infographic titled:

"TWO FRONT DOORS, ONE SKILL SET"

Visual story:
Show two entry paths at the top:

Left:
A browser/workbench UI card labeled:
"Web UI"
Subtitle: "user-friendly path"

Right:
An MCP client card labeled:
"MCP client"
Subtitle: "programmatic path"

Both paths converge into a shared vertical execution spine in the center.

Execution spine stages:
1. start_stage_run
2. build_prompt
3. Agent Harness
4. One Workbench Skill
5. Shared Outputs

Make "Agent Harness" the most visually prominent middle module, with amber/purple
styling and small icons for planning, tool use, and guardrails.

At the bottom, show shared outputs as three cards:
- Knowledge Graph
- Artifacts
- Review Gates

Include visual distinction:
- Web UI streams events
- MCP polls status

But keep the text minimal and visual.

Style:
Match the attached reference's polished matrix/flow style with rounded panels,
connector arrows, status dots, and small dimensional icons.
```

## Diagram 3 - Lifecycle Matrix

```text
Create a polished 16:9 infographic titled:

"DATA WORKBENCH MCP PROCESS LIFECYCLE"

Make it a rich flow matrix similar in spirit to the attached reference image.

Columns:
- MCP Tool
- Initialize Request
- Agent Harness
- Skill Execution
- Output / Review

Rows:
- Data Discovery
- Mapping & Transformation
- DQ Rule Generation
- Serving View
- Semantic Query

For each row, show a left-to-right flow using dots, arrows, and small icons.

Agent Harness column:
Highlight this column with amber/purple styling. Use icons for:
- intent parsing
- planning
- guardrails
- skill loading

Skill Execution column:
Use icons for:
- database scan
- profiling/rules
- mapping
- SQL/view generation
- semantic search

Output / Review column:
Use icons for:
- result table
- check shield
- review badge
- answer card

For Mapping & Transformation, make the final state visibly amber and label it:
"review gate"

Bottom:
Add a colorful band labeled:
"Server-side Skills Pool"

Style:
Light technical blueprint background, translucent main panel, polished 3D/vector
icons, colorful status dots, professional enterprise architecture slide. Avoid dense
tiny text.
```
