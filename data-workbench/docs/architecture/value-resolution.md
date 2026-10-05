# Value Resolution & Disambiguation (record-level lookups in Semantic Q&A)

> Formal name: **instance value resolution** — the text-to-SQL literature calls this
> sub-problem **value retrieval / cell-value linking**. This doc covers the Data
> Workbench implementation: `workbench/backend/value_resolution.py` plus its wiring
> into `marketplace_chat.py`, the two chat endpoints, the MCP tool, and the UI.
> It is a sub-capability of the marketplace Semantic Q&A (**`docs/architecture/semantic-qa.md`**)
> and changes nothing in the data-engineering pipeline.

---

## 1. The problem (why this exists)

The semantic layer (`:BusinessConcept` → `entity` → `attribute` → `value`) maps the
**shape** of the data: which products exist, which datasets/columns they expose, and
the business meaning of those columns. It deliberately does **not** model individual
**rows**. The lowest tier, `value`, only captures *low-cardinality enumerations*
(order status `placed|shipped|cancelled`, a country code) that were scaffolded from
profiling's top-values — never high-cardinality identifiers.

So the ontology can answer *"how many orders were cancelled by month?"* (status is a
modelled value) but has nothing to say about *"where does **John Doe** live?"* —
because **"John Doe" is a row, not a concept.** A person's name lives only in the
data; it is never a `:BusinessConcept`. When the question names a specific record by a
high-cardinality value, the normal NL→SQL path has two bad options:

1. **Guess** — the LLM invents `WHERE name = 'John Doe'` against a column it picked,
   often the wrong column, with the literal spelled however the user typed it (so a
   typo silently returns zero rows).
2. **Refuse** — "I can't find that concept", which is unhelpful when the record
   plainly exists in the data.

**In plain terms:** the system knows it has an "employee name" column, but it has no
idea whether *"John Doe"* (or *"Sophie Miller"*, or a near-miss spelling) is actually
*in* that column until it looks. The ontology gets you to the filing cabinet; it can't
tell you whether a particular file is inside.

---

## 2. What this feature adds (the value)

A query-time step that **resolves a named value against the live data** before SQL is
generated, and then either:

- **grounds** a *verified, exact* cell value into the NL→SQL prompt so the generated
  SQL filters on the real value (no guessing, no typo-induced empty results); or
- **disambiguates** — when the value isn't a unique, confident match it returns the
  top candidate records and lets the user pick (or asks which they meant), exactly the
  feedback loop a person expects: *"Did you mean **Sophia Miller — Support**?"*.

Concretely it delivers:

- **Record-level questions actually work** ("where does X live", "what is X's email").
- **Typo / spelling tolerance** — *"Sophie"* finds *"Sophia"* and offers it.
- **Self-correcting grounding** — the model is handed the exact stored literal, so it
  can't fabricate a value that returns nothing.
- **A transparent trace** — the Explain panel shows which value was probed, in which
  column, and what it resolved to.

No new graph nodes, no schema changes — resolution is **ephemeral** (computed per
question, never persisted), so it honours the "we don't model rows" design.

---

## 3. Prior art (the strategy is borrowed, not invented)

The approach mirrors how production text-to-SQL systems handle this:

- **[CHESS](https://arxiv.org/html/2405.16755v1)** — extract keywords/named-entities
  with an LLM, look them up against the *actual database values* (LSH + edit-distance +
  embedding), and inject the **verified exact cell values** back into the SQL-generation
  prompt as grounding. Our resolver is the same shape, adapted to query live Postgres
  views on demand instead of pre-building a value index.
- **[BRIDGE](https://arxiv.org/pdf/2408.05109)** — fuzzy "anchor text" matching of
  question spans against cell values.
- **[AmbiSQL](https://arxiv.org/html/2508.15276v2)** / **[Interactive Text-to-SQL via
  Expected Information Gain](https://arxiv.org/html/2507.06467v1)** — only ask a
  clarifying question when probing the data leaves *real* ambiguity, and ask it as a
  targeted multiple-choice.

The load-bearing principle we took from CHESS and hardened through iteration:
**the data is the arbiter, not the ranking.** Semantic ranking only decides *which*
columns to look in; whether a value lives in a column is settled by querying it.

---

## 4. Where it runs in the workflow

It is a step **inside the shared `marketplace_chat.answer_question()` pipeline**, so all
three Q&A modes (`full`, `concept_guided`, `conversational`) and the MCP
`query_semantic_layer` tool inherit it for free. Position:

```
gather_inputs
  └─[concept_guided only]→ decompose + extract value mentions → match concepts → narrow views
ensure_hydrated (columns + sample rows for the active view set)
  ★ VALUE RESOLUTION ★   ← runs here: after concept matching, before SQL generation
run_chat  (marketplace-product-chat-assistant skill; receives grounded_values)
validate_sql_allowlist_pairs
sql_executor.execute_select
synthesize_answer
```

It sits **between concept resolution and SQL generation** because (a) it needs the
hydrated column metadata to know column data types, and (b) its output (a verified
literal) is an *input* to SQL generation.

Per-mode integration:

- **`full` / `concept_guided`** (stateless `POST /api/marketplace/chat`) — if a value
  can't be uniquely resolved the endpoint returns `status: "needs_disambiguation"` with
  candidates; the UI shows chips and re-submits the pick via a `resolved_values` field.
- **`conversational`** (`POST /api/marketplace/conversation`) — the ontology-only router
  is untouched; a `needs_disambiguation` from the query pass is mapped to a **`clarify`
  turn** with the candidate labels as chips, and the pick is reconciled on the next turn
  from server-side `session_context.pending_disambiguation`.
- **MCP** — `query_semantic_layer` returns the candidate list as markdown and accepts a
  `resolved_value` on the follow-up call (or `auto_disambiguate=true` to auto-pick the
  top candidate in one shot for non-interactive agents).

---

## 5. How it's triggered (when the system decides it's needed)

Two gates, cheapest first, so analytic questions pay nothing:

1. **Pre-gate (no LLM): `should_resolve_values(user_message)`** — a regex/heuristic
   that returns `True` only when the message plausibly references a specific record:
   it contains a **quoted span**, a **trigger phrase** (`named`, `who is`, `where does`,
   `works in`, `lives in`, …), or a **proper noun** (a capitalised, non-sentence-initial,
   non-stopword token). A purely analytic question — *"orders by month"*, *"top 10
   products by revenue"* — has none of these and is skipped. *(Used in `full` mode,
   where no decompose pass runs.)*

2. **LLM extraction:** the existing concept-decompose pass is reused —
   `decompose_and_extract()` returns both the concept phrases **and**
   `value_mentions: [{text, type_hint}]` (e.g. `{"text": "John Doe", "type_hint":
   "person name"}`). In `concept_guided`/`conversational` this is free (the decompose
   pass already runs); in `full` mode it's a single small pass gated by step 1. For a
   purely analytic question the model returns `value_mentions: []` and resolution is a
   no-op.

So the trigger is: **the question contains a record-level value mention** (a named
person/place/company/id/…), as opposed to a concept/dimension/measure. Only then does
any data probing happen.

---

## 6. The resolution pipeline (step by step)

For each extracted mention (`value_resolution.resolve_mentions`):

### 6.1 Rank candidate columns — `rank_attribute_candidates()`
Build the candidate set from **every type-compatible column across the full deployed
view set** (`all_deployed_views`), concept-bound or not — because a person name is
rarely a `:BusinessConcept`, so restricting to bound attributes would miss it. Each
column is scored by the mention's **type** (`type_hint`) against the column/attribute
name (lexical token overlap + optional embedding cosine + an identifier-name bonus for
columns like `*_name`, `email`, `title`, `city`). Type-incompatible columns are dropped
(a textual name never considers a numeric/date column). Deduped by column, top
`PROBE_COLUMN_CAP` (10) returned.

> The ranking **never decides the answer** — it only picks which columns are worth
> looking in (a cost bound). This is the fix for the early "Sophie is a *Gender*" bug:
> an embedding may rank `Gender` near a name, but the data settles it.

### 6.2 Probe the data — `probe_column_for_value()`
For each ranked column, run a bounded, read-only lookup against the live Postgres view,
ranked in Python with `rapidfuzz`:

- **Hybrid matcher:** if the `pg_trgm` extension is present (`_has_pg_trgm`, cached per
  connection), use trigram `similarity()`/`%` (best typo tolerance). Otherwise a
  **typo-tolerant ILIKE prefilter**: each token matches on a shortened prefix
  (`_fuzzy_prefix`: *"Sophie"* → `%Soph%`) so trailing-character typos still prefilter
  in, then `rapidfuzz.WRatio` restores precision.
- Keep values scoring ≥ `PROBE_MIN_FUZZY` (78); attach up to `MAX_CONTEXT_COLS`
  identifying columns (e.g. department, email) for human-readable candidate labels.

### 6.3 Decide — across all probed columns
Flatten matches from every column the data supports, ranked by fuzzy score:

- **Auto-resolve** when the top match is **unique and near-exact** (`fuzzy ≥
  AUTO_RESOLVE_FUZZY` = 95 and only one distinct value within `RESOLVE_MARGIN` = 8 of
  the top) → bind that exact literal, no user interaction. *(An exact 100 match
  auto-resolves; a typo lands below 95 and is recommended instead — you confirm.)*
- **Record-ambiguous** otherwise → return the top `CANDIDATES_RETURNED` (5) **concrete
  candidate values**, each tagged with its field when matches span multiple columns
  (*"Sophia · First Name"* vs *"Sophia Miller — Support · Full Name"*). We surface
  concrete values, never an abstract "which column" question.
- **Not found** → no column held a match above the floor; return a graceful "couldn't
  find X — check the spelling or tell me which entity it belongs to."

A resolved mention becomes a `grounded_values` entry
`{mention_text, value, column_name, view_name, view_schema}`; an unresolved one builds
the `disambiguation` payload (one mention is resolved at a time for UI simplicity).

### 6.4 Hand off
- Resolved literals are injected into the `run_chat` payload as `grounded_values`. The
  `marketplace-product-chat-assistant` skill is instructed (hard rule) to filter on them
  **verbatim** — `WHERE "<view>"."<column>" = '<value>'` — never re-fuzzy or invent.
- An unresolved mention short-circuits the pipeline **before SQL is generated** with
  `status: "needs_disambiguation"`. The user's pick re-enters via `resolved_values`,
  which **skips extraction/probing** and grounds deterministically (a value-only pick
  grounds directly; an attribute/column pin re-probes just that column via
  `resolve_pins`).

The whole thing is recorded as a `ground_values` step in the Explain trace
(`decompose → match → resolve_views → ground_values → generate_sql → execute`).

---

## 7. Fallback mechanisms (graceful degradation everywhere)

The feature is **best-effort**: every failure path degrades to "behave as before",
never an error in the user's face.

| Component unavailable / fails | Fallback |
|---|---|
| `pg_trgm` not installed | typo-tolerant **prefix-ILIKE + rapidfuzz** (handles trailing typos; internal typos like *Jon→John* need pg_trgm) |
| `rapidfuzz` not installed | stdlib `difflib.SequenceMatcher` ratio |
| Embedding model unavailable | lexical + identifier-bonus ranking only |
| LLM extraction (SDK) unavailable/errs | returns no mentions → resolution is a no-op, normal NL→SQL proceeds |
| Resolution raises / times out | caught; the question proceeds **ungrounded** (old behaviour) |
| Mention can't be ranked to any column | `not_found` disambiguation (helpful message), not a crash |
| Conversational pick doesn't match a candidate | falls through to the router as a fresh turn |

Trigger-level fallback: the pre-gate + extraction mean a question with **no** value
mention skips the feature entirely (zero probes, zero added cost).

---

## 8. Safety & governance

- **Read-only + bounded + audited:** every probe routes through
  `sql_executor.execute_select` — read-only transaction, 30 s statement timeout, row cap
  (`PROBE_MAX_DISTINCT` = 200), and a `:QueryRun` audit row (`executedBy =
  "value-probe:<domain>"`).
- **Allow-list scoped:** a probe is rejected unless its `(schema, view)` is in the chat's
  `allowed_pairs`. Resolution searches the **full** deployed-view set (the concept
  narrowing only shrinks the *SQL-generation* context, not the allow-list), so the name
  column is reachable even on a "where does X live" question that narrowed to location
  views.
- **PII / suppression respected for free:** only deployed-view columns are probed, and
  deployed views already exclude `suppressedColumns` — so suppressed/PII columns are
  never searched and never appear in candidate labels.
- **Injection:** the mention literal is user input embedded into SQL text (because
  `execute_select` takes a SQL string). `_pg_text_literal` escapes single quotes;
  `_ilike_pattern_literal` escapes LIKE metacharacters (`% _ \`) with `ESCAPE '\'`;
  `_sanitize_mention` strips control chars and caps length/tokens. *(A related shared
  fix: `execute_select` now inlines the `LIMIT` as an int literal instead of a bind
  param, so a literal `%` in any query — ours or a skill's `LIKE '%x%'` — no longer
  trips psycopg2's parameter parser.)*
- **No graph writes** beyond the standard `:QueryRun` read-log. Nothing about the
  resolved record is persisted.

---

## 9. Worked example

**Question (conversational mode):** *"What is Sophie Miller's email address?"*
The data actually contains **"Sophia Miller"** (note the typo), and there are several
employees with that name.

1. **Trigger** — `should_resolve_values` sees the proper noun "Sophie Miller"; the
   decompose pass extracts `{"text": "Sophie", "type_hint": "person name"}`.
2. **Rank** — across all deployed views, type-compatible text columns rank highest where
   the field is name-like: `vw_employee.first_name`, `vw_active_employees.full_name`,
   `vw_employee.last_name`, … (`Gender`, `City`, etc. rank low / get eliminated by the
   data).
3. **Probe** — no `pg_trgm`, so prefix-ILIKE: `first_name ILIKE '%Soph%'` matches
   *"Sophia"* (rapidfuzz `"Sophie"`↔`"Sophia"` ≈ 83); `full_name ILIKE '%Soph%'` matches
   *"Sophia Miller"*.
4. **Decide** — top match is ~83–92, **below** the 95 auto-resolve bar → **record-
   ambiguous**. Two concrete candidates, tagged by field:
   - `Sophia — sophia.miller149@example.com, Miller · First Name`
   - `Sophia Miller — Support, sophia.miller9@example.com · Full Name`
5. **Disambiguate** — returned as a `clarify` turn; the UI renders the two as chips.
6. **Pick** — the user clicks *"Sophia Miller … · Full Name"*. The next turn reconciles
   the pick against `session_context.pending_disambiguation` (chip label → exact match;
   *"the first one"* / *"#2"* / *"yes that's her"* also work) and re-runs the query with
   `resolved_values=[{value: "Sophia Miller", column_name: "full_name", …}]`.
7. **Answer** — extraction is skipped; the verified literal is grounded; the skill emits
   SQL filtering on the real value and returns Sophia Miller's email(s). The Explain
   panel shows the `ground_values` step: `Sophie Miller → full_name = 'Sophia Miller'`.

**Plain-terms recap:** you asked about a person by a slightly-misspelled name; the system
went and *looked in the data*, found the closest real people, showed them to you with
enough context to tell them apart, and — once you pointed at the right one — answered
using that exact record. The ontology never had to know who "Sophie Miller" was.

---

## 10. Key files & functions

| File | Role |
|---|---|
| `workbench/backend/value_resolution.py` | the resolver — `should_resolve_values`, `rank_attribute_candidates`, `probe_column_for_value` (hybrid pg_trgm/`_fuzzy_prefix` ILIKE), `_decide_from_candidates`, `_resolve_one`, `resolve_mentions`, `resolve_pins`; escaping (`_pg_text_literal`, `_ilike_pattern_literal`, `_sanitize_mention`); thresholds |
| `workbench/backend/marketplace_chat.py` | `decompose_and_extract` / `extract_value_mentions`; `ChatInputs.{value_mentions, grounded_values, all_deployed_views, all_concepts}`; the resolution step + `needs_disambiguation` return in `answer_question`; `grounded_values` in `run_chat`; `_hydrate_columns_only`; `ground_values` step in `build_trace` |
| `workbench/backend/routers/marketplace.py` | `/chat` `resolved_values`; `/conversation` `pending_disambiguation` + `_match_pending_pick` (label / ordinal / affirmative) + `_disambiguation_to_clarify` |
| `workbench/backend/mcp_server.py` | `query_semantic_layer` `resolved_value` / `auto_disambiguate` |
| `workbench/backend/sql_executor.py` | `execute_select` — the gated probe executor (read-only/timeout/limit/audit; LIMIT-as-literal fix) |
| `workbench-skills/skills/marketplace-product-chat-assistant/SKILL.md` | the `grounded_values` input contract + "use verbatim" hard rule + worked example |
| `workbench/frontend/src/components/MarketplaceChatPanel.tsx` | disambiguation candidate/attribute chips + re-submit; `ground_values` Explain step |
| `workbench/backend/tests/test_value_resolution.py`, `test_semantic_conversation.py` | unit coverage (escaping, gating, ranking, the four decision branches, data-overrides-ranking regression, conversational reconcile) |

## 11. Tuning knobs (module constants in `value_resolution.py`)

`PROBE_MIN_FUZZY=78` (keep floor) · `AUTO_RESOLVE_FUZZY=95` (silent-resolve bar) ·
`RESOLVE_MARGIN=8` (uniqueness margin) · `COLUMN_DOMINANCE_MARGIN=6` · `PROBE_COLUMN_CAP=10`
(columns probed/mention) · `PROBE_MAX_DISTINCT=200` (rows/probe) · `CANDIDATES_RETURNED=5`
· `MAX_MENTION_LEN=128` · `MAX_TOKENS=8`.

## 12. Known soft spots / future work

- **Extraction granularity** — the LLM sometimes shortens *"Sophie Miller"* → *"Sophie"*,
  producing two candidates (first-name + full-name) instead of one clean full-name hit.
  Tightening the decompose prompt to keep the full name span would make these single-hit.
- **Internal/leading typos** (*Jon→John*, *Sofia→Sophia*) need `pg_trgm`; the prefix-ILIKE
  fallback only covers trailing typos. Enabling `pg_trgm` on the deployed Postgres makes
  the hybrid matcher use it automatically.
- **Resolved-entity memory** — a follow-up like *"her salary"* currently re-resolves from
  scratch; carrying the resolved record in the conversational `session_context` would let
  the router reference it without re-disambiguating.
