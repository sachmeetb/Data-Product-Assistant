"""
main.py — starts the AI Retail Data Agent backend API.

Usage:
  Terminal 1 — Backend:
    cd backend
    python main.py          (or: uvicorn server:app --reload --port 8000)

  Terminal 2 — Frontend:
    cd frontend
    npm run dev             (opens http://localhost:5173)
"""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).parent


def main():
    load_dotenv(HERE / ".env")

    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        print("Error: GOOGLE_CLOUD_PROJECT must be set in .env")
        sys.exit(1)

    print("Starting AI Retail Data Agent backend on http://localhost:8000 ...")
    print("Start the frontend separately:  cd frontend && npm run dev")
    print("-" * 55)

    try:
        subprocess.run(
            [sys.executable, "-m", "uvicorn", "server:app", "--reload", "--port", "8000"],
            cwd=str(HERE),
        )
    except KeyboardInterrupt:
        print("\nBackend stopped.")


if __name__ == "__main__":
    main()
