"""
domain_scope.py — Domain whitelist for the Requirements Agent POC guardrail.

Three tiers:
  GREEN — in-scope, proceed normally
  AMBER — related domain, proceed with an advisory note
  RED   — out of scope, block and show "coming soon" message

classify_domain(domain_str) -> (tier, canonical_name, advisory_note)
"""

from __future__ import annotations

_GREEN: list[dict] = [
    {
        "name": "C&P",
        "keywords": ["c&p", "c & p", "consumer", "consumers and products",
                     "consumers & products", "consumers and product"],
    },
    {
        "name": "Sales",
        "keywords": ["sales", "sell-through", "sell through", "commercial"],
    },
    {
        "name": "Category Management",
        "keywords": ["category management", "category", "cat man", "catman"],
    },
    {
        "name": "Pricing",
        "keywords": ["pricing", "price management", "price optimisation",
                     "price optimization"],
    },
    {
        "name": "Merchandising",
        "keywords": ["merchandising", "merch", "planogram", "shelf space",
                     "assortment", "ranging"],
    },
    {
        "name": "Marketing",
        "keywords": ["marketing", "brand", "campaign", "go-to-market", "gtm"],
    },
    {
        "name": "Advertising",
        "keywords": [
            "advertising", "advert", "ad spend", "paid media",
            "media buying", "digital marketing",
            # Ad-tech / campaign performance vocabulary
            "impression", "impressions", "click", "clicks", "conversion", "conversions",
            "ad-tech", "adtech", "programmatic", "dsp", "dv360",
            "google ads", "meta ads", "facebook ads", "display",
            "ctr", "cpc", "cpa", "roas", "creative", "placement",
            "line item", "attribution", "landing page",
        ],
    },
    {
        "name": "Trade Promotions",
        "keywords": ["trade promotion", "trade spend", "tpm", "promotional spend",
                     "promotion"],
    },
    {
        "name": "Loyalty & Rewards",
        "keywords": ["loyalty", "reward", "retention", "repeat purchase",
                     "loyalty programme", "loyalty program",
                     # Customer-retention / Customer-Success synonyms — these
                     # are the SaaS/B2B vocabulary the LLM tends to reach for
                     # when classifying churn, renewals, and account work.
                     # In a CPG/retail (Accenture C&P) context these all sit under
                     # Loyalty & Rewards, so route them in.
                     "customer success", "account management", "renewals",
                     "renewal", "win-back", "win back", "subscriber retention",
                     "churn", "churn prediction", "churn prevention",
                     "customer retention"],
    },
    {
        "name": "Digital Commerce",
        "keywords": ["digital commerce", "ecommerce", "e-commerce",
                     "online commerce", "digital sales", "online sales"],
    },
    {
        "name": "Customer Experience",
        "keywords": ["customer experience", "cx", "nps", "net promoter",
                     "satisfaction", "complaints", "customer service"],
    },
    {
        "name": "Consumer Insights",
        "keywords": ["consumer insight", "market research", "market share",
                     "demand sensing", "insights", "consumer research"],
    },
]

_AMBER: list[dict] = [
    {
        "name": "Supply Chain",
        "keywords": ["supply chain", "logistics", "fulfilment", "fulfillment",
                     "inventory", "warehouse", "distribution", "procurement"],
        "note": (
            "Supply Chain spans multiple domains. Ensure your request is scoped "
            "to consumer or product impact before proceeding."
        ),
    },
    {
        "name": "Finance",
        "keywords": ["finance", "financial", "p&l", "profit and loss",
                     "revenue reporting", "cost management", "budget", "forecast"],
        "note": (
            "Finance data is broad in scope. Ensure your request is linked to "
            "a specific product line or consumer segment."
        ),
    },
]

_OUT_OF_SCOPE_MESSAGE = (
    "This use case is outside the current scope of the Data Product Assistant.\n\n"
    "The assistant currently supports **C&P and related domains** — including "
    "Sales, Marketing, Pricing, Loyalty, Category Management, Merchandising, "
    "Advertising, Trade Promotions, Digital Commerce, Customer Experience, "
    "and Consumer Insights.\n\n"
    "Support for **{domain}** is coming soon. "
    "Please revise your requirement or speak to your administrator."
)


_UNKNOWN_SIGNALS = {"unknown", "n/a", "not sure", "don't know", "do not know", "none", "null"}


def classify_domain(domain: str) -> tuple[str, str, str]:
    """
    Classify a user-supplied domain string against the POC whitelist.

    Returns:
        tier          — "green" | "amber" | "red"
        canonical     — matched canonical domain name, or the original string
        advisory_note — non-empty for amber, empty string otherwise

    When domain is null, empty, or marked unknown by the user, returns "green"
    (no restriction applied). The mandatory field handoff gate handles the
    missing domain case separately — domain scope restriction only fires when
    a domain is explicitly provided and is not in the whitelist.
    """
    if not domain:
        return "green", "", ""

    d = domain.lower().strip()

    if d in _UNKNOWN_SIGNALS:
        return "green", domain, ""

    for entry in _GREEN:
        if any(kw in d for kw in entry["keywords"]):
            return "green", entry["name"], ""

    for entry in _AMBER:
        if any(kw in d for kw in entry["keywords"]):
            return "amber", entry["name"], entry["note"]

    return "red", domain, ""


def out_of_scope_message(domain: str) -> str:
    """Return the user-facing block message for a Red-tier domain."""
    return _OUT_OF_SCOPE_MESSAGE.format(domain=domain or "this domain")
