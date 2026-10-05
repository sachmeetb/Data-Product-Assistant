"""Test script for E5: Agent-to-User Messaging.

Tests the full flow in-process using the FastAPI test client so the
message queue is shared between "agent" and "user" sides.

Usage:
    source env/bin/activate
    python scripts/test_agent_messaging.py
"""

import asyncio
import json
import sys
import uuid

sys.path.insert(0, ".")

from workbench.backend.message_queue import message_queue


async def test_multiple_choice():
    """Test multiple choice question → response flow."""
    print("=== Test: Multiple Choice ===")

    run_id = str(uuid.uuid4())
    ws_messages = []

    async def fake_ws_send(msg):
        ws_messages.append(msg)

    message_queue.register_run(run_id, fake_ws_send)

    # Agent posts a question (background — it blocks)
    async def agent_asks():
        from httpx import AsyncClient, ASGITransport
        from workbench.backend.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agent/ask", json={
                "run_id": run_id,
                "message_type": "multiple_choice",
                "prompt": "Which dataset should I profile first?",
                "context": "Found 3 datasets in the schema.",
                "options": [
                    {"value": "employees", "label": "Employees", "description": "HR employee records"},
                    {"value": "departments", "label": "Departments", "description": "Org structure"},
                    {"value": "salaries", "label": "Salaries", "description": "Compensation data"},
                ],
                "timeout_seconds": 10,
            })
            return resp.json()

    agent_task = asyncio.create_task(agent_asks())
    await asyncio.sleep(0.5)

    # Check the question was relayed to "frontend"
    assert len(ws_messages) == 1, f"Expected 1 WS message, got {len(ws_messages)}"
    q = ws_messages[0]
    assert q["type"] == "agent_question"
    assert q["message_type"] == "multiple_choice"
    assert len(q["options"]) == 3
    print(f"  [WS→UI] agent_question: {q['prompt']}")
    print(f"  [WS→UI] options: {[o['value'] for o in q['options']]}")

    # User responds
    question_id = q["question_id"]
    print(f"  [User]  Responding: employees")
    await message_queue.post_response(run_id, question_id, "employees")

    # Agent gets the response
    result = await agent_task
    print(f"  [Agent] Got: {json.dumps(result)}")
    assert result["response"] == "employees"
    assert result["timed_out"] is False

    message_queue.cleanup_run(run_id)
    print("  PASSED")


async def test_yes_no():
    """Test yes/no question type."""
    print("\n=== Test: Yes/No ===")

    run_id = str(uuid.uuid4())
    ws_messages = []

    async def fake_ws_send(msg):
        ws_messages.append(msg)

    message_queue.register_run(run_id, fake_ws_send)

    async def agent_asks():
        from httpx import AsyncClient, ASGITransport
        from workbench.backend.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agent/ask", json={
                "run_id": run_id,
                "message_type": "yes_no",
                "prompt": "Should I proceed with all 15 tables?",
                "context": "This may take several minutes.",
                "timeout_seconds": 10,
            })
            return resp.json()

    agent_task = asyncio.create_task(agent_asks())
    await asyncio.sleep(0.5)

    q = ws_messages[0]
    print(f"  [WS→UI] agent_question: {q['prompt']}")
    print(f"  [User]  Responding: yes")
    await message_queue.post_response(run_id, q["question_id"], "yes")

    result = await agent_task
    print(f"  [Agent] Got: {json.dumps(result)}")
    assert result["response"] == "yes"

    message_queue.cleanup_run(run_id)
    print("  PASSED")


async def test_free_text():
    """Test free text input."""
    print("\n=== Test: Free Text ===")

    run_id = str(uuid.uuid4())
    ws_messages = []

    async def fake_ws_send(msg):
        ws_messages.append(msg)

    message_queue.register_run(run_id, fake_ws_send)

    async def agent_asks():
        from httpx import AsyncClient, ASGITransport
        from workbench.backend.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agent/ask", json={
                "run_id": run_id,
                "message_type": "free_text",
                "prompt": "What schema name should I use?",
                "timeout_seconds": 10,
            })
            return resp.json()

    agent_task = asyncio.create_task(agent_asks())
    await asyncio.sleep(0.5)

    q = ws_messages[0]
    print(f"  [WS→UI] agent_question: {q['prompt']}")
    print(f"  [User]  Responding: hr_analytics")
    await message_queue.post_response(run_id, q["question_id"], "hr_analytics")

    result = await agent_task
    print(f"  [Agent] Got: {json.dumps(result)}")
    assert result["response"] == "hr_analytics"

    message_queue.cleanup_run(run_id)
    print("  PASSED")


async def test_timeout_with_default():
    """Test timeout falls back to default value."""
    print("\n=== Test: Timeout with Default ===")

    run_id = str(uuid.uuid4())
    ws_messages = []

    async def fake_ws_send(msg):
        ws_messages.append(msg)

    message_queue.register_run(run_id, fake_ws_send)

    async def agent_asks():
        from httpx import AsyncClient, ASGITransport
        from workbench.backend.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agent/ask", json={
                "run_id": run_id,
                "message_type": "free_text",
                "prompt": "Custom schema name?",
                "default_value": "public",
                "timeout_seconds": 2,
            })
            return resp.json()

    print("  [Agent] Asking with 2s timeout, default='public'...")
    result = await agent_asks()

    # Should have question + timeout messages
    assert any(m["type"] == "agent_question" for m in ws_messages)
    assert any(m["type"] == "agent_timeout" for m in ws_messages)

    print(f"  [Agent] Got: {json.dumps(result)}")
    assert result["timed_out"] is True
    assert result["response"] == "public"

    message_queue.cleanup_run(run_id)
    print("  PASSED")


async def test_notification():
    """Test notification (no response expected)."""
    print("\n=== Test: Notification ===")

    run_id = str(uuid.uuid4())
    ws_messages = []

    async def fake_ws_send(msg):
        ws_messages.append(msg)

    message_queue.register_run(run_id, fake_ws_send)

    async def agent_notifies():
        from httpx import AsyncClient, ASGITransport
        from workbench.backend.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/agent/ask", json={
                "run_id": run_id,
                "message_type": "notification",
                "prompt": "Discovery phase completed. Found 5 tables.",
                "timeout_seconds": 2,
            })
            return resp.json()

    # Notification will timeout (no response expected) — that's fine
    result = await agent_notifies()
    assert any(m["type"] == "agent_question" for m in ws_messages)
    print(f"  [WS→UI] Notification delivered: {ws_messages[0]['prompt']}")
    print(f"  [Agent] Got: {json.dumps(result)}")

    message_queue.cleanup_run(run_id)
    print("  PASSED")


async def main():
    await test_multiple_choice()
    await test_yes_no()
    await test_free_text()
    await test_timeout_with_default()
    await test_notification()
    print("\n" + "=" * 40)
    print("All tests passed!")
    print("=" * 40)


if __name__ == "__main__":
    asyncio.run(main())
