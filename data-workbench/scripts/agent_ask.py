#!/usr/bin/env python3
"""Agent-to-user messaging helper. Agents call this instead of curl to
ask questions of the user via the workbench UI.

Usage:
  python /path/to/agent_ask.py --run-id <RUN_ID> --type <TYPE> --prompt "Question?"

Message types:
  notification     - informational, no response expected
  free_text        - open-ended text input
  yes_no           - binary yes/no choice
  multiple_choice  - single-select from options (radio buttons)
  checklist        - multi-select from options (checkboxes)

Options (for multiple_choice / checklist):
  --option value:label           Add an option (repeat for each)
  --option value:label:description  Add an option with description

Other flags:
  --context "extra context"      Shown below the prompt
  --default "fallback value"     Used if user doesn't respond in time
  --timeout 300                  Seconds to wait (default: 300)

Examples:
  python agent_ask.py --run-id abc123 --type yes_no --prompt "Continue?"

  python agent_ask.py --run-id abc123 --type checklist \
    --prompt "Which schemas?" \
    --option "public:Public:Main schema" \
    --option "hr:HR:Human resources"

Output:
  Prints the user's response text to stdout. For checklist, the response
  is comma-separated values (e.g. "public, hr").
"""

import argparse
import json
import sys
import urllib.request
import urllib.error


def main():
    parser = argparse.ArgumentParser(description="Ask the user a question via the workbench UI")
    parser.add_argument("--run-id", required=True, help="Stage run ID")
    parser.add_argument("--type", required=True,
                        choices=["notification", "free_text", "yes_no", "multiple_choice", "checklist"],
                        help="Message type")
    parser.add_argument("--prompt", required=True, help="Question prompt")
    parser.add_argument("--context", default=None, help="Additional context")
    parser.add_argument("--default", default=None, help="Default value on timeout")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds (default: 300)")
    parser.add_argument("--option", action="append", default=[],
                        help="Option as value:label or value:label:description (repeatable)")
    parser.add_argument("--host", default="localhost", help="Workbench host (default: localhost)")
    parser.add_argument("--port", default="8000", help="Workbench port (default: 8000)")
    args = parser.parse_args()

    # Build options list
    options = []
    for opt_str in args.option:
        parts = opt_str.split(":", 2)
        entry = {"value": parts[0], "label": parts[1] if len(parts) > 1 else parts[0]}
        if len(parts) > 2:
            entry["description"] = parts[2]
        options.append(entry)

    # Build payload
    payload = {
        "run_id": args.run_id,
        "message_type": args.type,
        "prompt": args.prompt,
        "timeout_seconds": args.timeout,
    }
    if args.context:
        payload["context"] = args.context
    if args.default:
        payload["default_value"] = args.default
    if options:
        payload["options"] = options

    # Send request
    url = f"http://{args.host}:{args.port}/api/agent/ask"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=args.timeout + 30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            response_text = result.get("response", "")
            if result.get("timed_out"):
                print(f"[timed out, using default: {response_text}]", file=sys.stderr)
            print(response_text)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Error {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
