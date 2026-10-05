export interface SuggestedPrompt {
  label: string;
  prompt: string;
}

export const PROJECT_OVERVIEW_PROMPTS: SuggestedPrompt[] = [
  {
    label: "Which columns have no approved description?",
    prompt: "Which columns in this project do not yet have an approved description? Show me the top 20, grouped by table.",
  },
  {
    label: "Which tables have failing DQ tests?",
    prompt: "In the most recent DQ test run, which tables had failing tests? Include the number of failing expectations per table.",
  },
  {
    label: "Top null-rate columns",
    prompt: "Show the top 20 columns by null rate across the project.",
  },
  {
    label: "Rules breakdown by source and status",
    prompt: "Give me the count of DQ rules broken down by ruleSource (observation vs domain) and status.",
  },
  {
    label: "Domain rules pending review",
    prompt: "List the domain rules that are pending review, grouped by table.",
  },
];

export const STAGE_SCORING_PROMPTS: SuggestedPrompt[] = [
  {
    label: "Current dataset scores",
    prompt: "Show the latest composite score and each dimension score per dataset, using the most recent batch.",
  },
  {
    label: "Biggest score drops",
    prompt: "Which datasets had the largest drop in composite score between the last two scoring batches?",
  },
  {
    label: "What is Grounding?",
    prompt: "Explain the Grounding dimension. What evidence feeds it, how is it weighted, and what would raise a low score?",
  },
  {
    label: "Why is Documentation low?",
    prompt: "Look at Documentation dimension scores. Which datasets are lowest, and what state are their :ColumnDescription nodes in?",
  },
];

export const STAGE_RULES_PROMPTS: SuggestedPrompt[] = [
  {
    label: "Observation vs domain rules",
    prompt: "What is the difference between observation rules and domain rules in this workbench? How do I add or edit each?",
  },
  {
    label: "Columns with rules but no description",
    prompt: "Which columns have at least one DQ rule but no approved description?",
  },
  {
    label: "Rule coverage by table",
    prompt: "What fraction of columns per table have at least one rule? Show the tables with the lowest coverage.",
  },
];

export const STAGE_TESTS_PROMPTS: SuggestedPrompt[] = [
  {
    label: "Latest test run summary",
    prompt: "Summarise the most recent DQ test run: framework, total expectations, passed, failed.",
  },
  {
    label: "Failing expectations with context",
    prompt: "List the failing expectations in the last DQ test run with their column, rule type, and pass rate.",
  },
  {
    label: "What does the DQ Failure Analysis do?",
    prompt: "What does the DQ Failure Analysis stage add on top of the test execution results?",
  },
];

export const STAGE_MAPPING_PROMPTS: SuggestedPrompt[] = [
  {
    label: "Unmapped product columns",
    prompt: "For each data product in this project, which product columns do not yet have an approved mapping?",
  },
  {
    label: "Low-confidence mappings",
    prompt: "List the current column mappings with similarity score below 0.6 and their source/target columns.",
  },
  {
    label: "Mappings broken down by transform author",
    prompt: "Show counts of current mappings grouped by transformAuthor (po_hint, ai_suggestion, engineer, steward_catalog).",
  },
  {
    label: "Mappings awaiting steward",
    prompt: "Which mappings are in status='steward_review'? Show product column, source columns, transformEscalationReason.",
  },
];

export const REVIEW_MAPPINGS_PROMPTS: SuggestedPrompt[] = [
  {
    label: "Suggest a transformation for this column",
    prompt: "I'm reviewing a mapping. Suggest a transform_kind and expression for the current product column. Tell me which one I'm on first.",
  },
  {
    label: "What kind of transform fits here?",
    prompt: "Given the source columns and the product column description, which transform_kind (direct, concat, cast, lookup, bucket, mask, hash, expression) is the best fit and why?",
  },
  {
    label: "Should this column be masked or hashed?",
    prompt: "Look at this product column's description and PII classification. Should it use the mask, hash, or neither kind? If mask, recommend the algorithm and keep_n; if hash, recommend the algorithm.",
  },
  {
    label: "Is this column a bucket candidate?",
    prompt: "Could this product column be expressed as a bucket of a continuous source column? Suggest boundaries and labels if so.",
  },
  {
    label: "Lookup with selection_strategy=latest?",
    prompt: "Does this product column reference a history table? If so, should it use a lookup with selection_strategy=latest? Recommend order_by_column.",
  },
  {
    label: "Are there steward catalog templates that match?",
    prompt: "Run match_transformation_catalog.py against the current product column and report any matches.",
  },
];

export const REVIEW_DESCRIPTIONS_PROMPTS: SuggestedPrompt[] = [
  {
    label: "Evidence for a pending description",
    prompt: "What profiling evidence and rules exist for the column whose description I'm reviewing? (tell me the column first)",
  },
  {
    label: "Recently rejected descriptions",
    prompt: "Which descriptions were rejected recently, and what rejection reasons did reviewers give?",
  },
];

export const REVIEW_DOMAIN_RULES_PROMPTS: SuggestedPrompt[] = [
  {
    label: "Why was this rule proposed?",
    prompt: "For a pending domain rule, show me the column's profiling measurements and any conflicting observation rule.",
  },
  {
    label: "Domain rule summary",
    prompt: "Summarise the pending domain rules by rule type and severity.",
  },
];

export const WORKBENCH_HELP_PROMPTS: SuggestedPrompt[] = [
  {
    label: "How do I add a domain rule?",
    prompt: "How do I add a domain rule in the workbench? Walk me through the stages and what happens after I run them.",
  },
  {
    label: "Difference between workflows and stages",
    prompt: "Explain the difference between workflows and stages in a project. When do I add a new workflow vs run a repeat of an existing one?",
  },
  {
    label: "What each review type does",
    prompt: "What are the three review types (descriptions, mappings, domain rules), and who approves each?",
  },
];

export interface ChatContextKey {
  kind: "overview" | "stage" | "review";
  stageId?: string;
  reviewType?: "descriptions" | "mappings" | "domain_rules" | "source_product_validation";
}

export function promptsForContext(ctx: ChatContextKey): SuggestedPrompt[] {
  if (ctx.kind === "review" && ctx.reviewType === "descriptions") {
    return REVIEW_DESCRIPTIONS_PROMPTS;
  }
  if (ctx.kind === "review" && ctx.reviewType === "domain_rules") {
    return REVIEW_DOMAIN_RULES_PROMPTS;
  }
  if (ctx.kind === "review" && ctx.reviewType === "mappings") {
    return REVIEW_MAPPINGS_PROMPTS;
  }
  if (ctx.kind === "stage" && ctx.stageId) {
    if (ctx.stageId.startsWith("data_scoring") || ctx.stageId === "rescore_composite") {
      return STAGE_SCORING_PROMPTS;
    }
    if (ctx.stageId.startsWith("dq_rule")) {
      return STAGE_RULES_PROMPTS;
    }
    if (ctx.stageId.startsWith("dq_test") || ctx.stageId === "dq_failure_analysis") {
      return STAGE_TESTS_PROMPTS;
    }
    if (ctx.stageId === "data_mapping" || ctx.stageId.startsWith("serving_")) {
      return STAGE_MAPPING_PROMPTS;
    }
  }
  return [...PROJECT_OVERVIEW_PROMPTS, ...WORKBENCH_HELP_PROMPTS].slice(0, 6);
}
