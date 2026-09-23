import json
from .base import run_agent


def run(discovery_output: dict, context: dict = {}) -> dict:
    """
    Receives the structured JSON output from the discovery agent.
    Generates a Mermaid diagram + markdown summary tables.
    Returns: { "raw_output": "<markdown string>" }
    because the visual agent produces text, not JSON.
    """
    user_input = json.dumps(discovery_output, indent=2)
    return run_agent("visual-diagram", user_input, context)
