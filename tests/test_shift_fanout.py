"""
Tests for the shift fanout service.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from freezegun import freeze_time
from httpx import ASGITransport, AsyncClient

from app import database, models
from app.api import create_app

# Load sample data
SAMPLE_DATA_PATH = Path(__file__).parent.parent / "sample_data.json"
with open(SAMPLE_DATA_PATH) as f:
    SAMPLE_DATA = json.load(f)


@pytest_asyncio.fixture
async def client():
    """
    Test fixture that creates an async client for the API.
    """
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def sample_data(client: AsyncClient):
    """
    Fixture that loads sample data into the database before each test.
    """
    # Clear databases
    database.shifts_db.clear()
    database.caregivers_db.clear()
    database.fanout_states_db.clear()

    # Load caregivers
    for cg_data in SAMPLE_DATA["caregivers"]:
        caregiver = models.Caregiver(**cg_data)
        database.caregivers_db.put(caregiver.id, caregiver)

    # Load shifts
    for shift_data in SAMPLE_DATA["shifts"]:
        shift = models.Shift(**shift_data)
        database.shifts_db.put(shift.id, shift)

    yield

    # Cleanup
    database.shifts_db.clear()
    database.caregivers_db.clear()
    database.fanout_states_db.clear()


@pytest_asyncio.fixture
async def mock_notifier():
    """
    Fixture that mocks the notifier functions to make them instant.
    """
    # Patch where it's used (app.api imports notifier)
    with patch("app.api.notifier.send_sms", new_callable=AsyncMock) as mock_sms, patch(
        "app.api.notifier.place_phone_call", new_callable=AsyncMock
    ) as mock_call:
        # Make mocks return immediately (no sleep)
        mock_sms.return_value = None
        mock_call.return_value = None
        yield {"send_sms": mock_sms, "place_phone_call": mock_call}


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient) -> None:
    """
    Test the health check endpoint.
    """
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_fanout_shift_success(
    client: AsyncClient, sample_data, mock_notifier
) -> None:
    """
    Test successful fanout for a shift.
    """
    shift_id = "f5a9d844-ecff-4f7a-8ef7-d091f22ad77e"  # RN shift

    response = await client.post(f"/shifts/{shift_id}/fanout")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"

    # Verify fanout state was created
    fanout_state = database.fanout_states_db.get(shift_id)
    assert fanout_state is not None
    assert fanout_state.round_1_sent is True
    assert fanout_state.round_2_sent is False


@pytest.mark.asyncio
async def test_fanout_shift_not_found(client: AsyncClient, sample_data) -> None:
    """
    Test fanout for non-existent shift returns 404.
    """
    response = await client.post("/shifts/non-existent-shift/fanout")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_fanout_shift_idempotent(
    client: AsyncClient, sample_data, mock_notifier
) -> None:
    """
    Test that duplicate fanout requests are idempotent.
    """
    shift_id = "f5a9d844-ecff-4f7a-8ef7-d091f22ad77e"

    # First request
    response1 = await client.post(f"/shifts/{shift_id}/fanout")
    assert response1.status_code == 200

    # Second request (should be idempotent)
    response2 = await client.post(f"/shifts/{shift_id}/fanout")
    assert response2.status_code == 200
    assert "already initiated" in response2.json()["message"].lower()


@pytest.mark.asyncio
async def test_fanout_shift_role_filtering(
    client: AsyncClient, sample_data, mock_notifier
) -> None:
    """
    Test that only caregivers with matching role are contacted.
    """
    shift_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"  # LPN shift

    response = await client.post(f"/shifts/{shift_id}/fanout")
    assert response.status_code == 200

    # Verify only LPN caregivers were contacted
    fanout_state = database.fanout_states_db.get(shift_id)
    assert fanout_state is not None
    contacted_ids = fanout_state.contacted_caregiver_ids
    assert len(contacted_ids) == 2  # Wei and Barry (both LPN)
    assert "b7e6a0f4-4c32-44dd-8a6d-ec6b7e9477da" in contacted_ids  # Wei
    assert "c3d4e5f6-g7h8-9012-cdef-345678901234" in contacted_ids  # Barry


@pytest.mark.asyncio
async def test_inbound_message_accept(
    client: AsyncClient, sample_data, mock_notifier
) -> None:
    """
    Test successful shift acceptance via inbound message.
    """
    shift_id = "f5a9d844-ecff-4f7a-8ef7-d091f22ad77e"  # RN shift

    # First, trigger fanout
    await client.post(f"/shifts/{shift_id}/fanout")

    # Send accept message from Alice (RN)
    message = {
        "from_phone": "+15550001",  # Alice
        "to_phone": "+15550000",
        "body": "yes, I accept",
    }

    response = await client.post("/messages/inbound", json=message)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"

    # Verify shift is claimed
    shift = database.shifts_db.get(shift_id)
    assert shift.status == models.ShiftStatus.CLAIMED
    assert shift.claimed_by == "27e8d156-7fee-4f79-94d7-b45d306724d4"  # Alice


@pytest.mark.asyncio
async def test_inbound_message_decline_ignored(
    client: AsyncClient, sample_data, mock_notifier
) -> None:
    """
    Test that decline messages are ignored.
    """
    shift_id = "f5a9d844-ecff-4f7a-8ef7-d091f22ad77e"

    # Trigger fanout
    await client.post(f"/shifts/{shift_id}/fanout")

    # Send decline message
    message = {
        "from_phone": "+15550001",  # Alice
        "to_phone": "+15550000",
        "body": "no, I decline",
    }

    response = await client.post("/messages/inbound", json=message)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "acknowledged"

    # Verify shift is still pending
    shift = database.shifts_db.get(shift_id)
    assert shift.status == models.ShiftStatus.PENDING


@pytest.mark.asyncio
async def test_inbound_message_unknown_phone(client: AsyncClient, sample_data) -> None:
    """
    Test message from unknown phone number returns 404.
    """
    message = {
        "from_phone": "+15559999",  # Not in sample data
        "to_phone": "+15550000",
        "body": "yes",
    }

    response = await client.post("/messages/inbound", json=message)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_inbound_message_no_pending_shifts(client: AsyncClient, sample_data) -> None:
    """
    Test message when caregiver has no pending shifts.
    """
    message = {
        "from_phone": "+15550001",  # Alice
        "to_phone": "+15550000",
        "body": "yes",
    }

    # Don't trigger fanout - no shifts available
    response = await client.post("/messages/inbound", json=message)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "no_action"


@pytest.mark.asyncio
async def test_escalation_after_10_minutes(
    client: AsyncClient, sample_data, mock_notifier
) -> None:
    """
    Test that escalation (phone calls) happens after 10 minutes.
    """
    shift_id = "f5a9d844-ecff-4f7a-8ef7-d091f22ad77e"

    # Mock asyncio.sleep to make escalation instant
    with patch("app.api.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        # Trigger fanout (this creates the escalation task)
        response = await client.post(f"/shifts/{shift_id}/fanout")
        assert response.status_code == 200

        # Verify round 1 sent, round 2 not sent
        fanout_state = database.fanout_states_db.get(shift_id)
        assert fanout_state.round_1_sent is True
        assert fanout_state.round_2_sent is False

        # Get the escalation task
        escalation_task = database.get_escalation_task(shift_id)
        assert escalation_task is not None
        assert not escalation_task.done()

        # Wait for the escalation task to complete
        # The mocked sleep will return immediately, so the task should complete quickly
        await escalation_task

        # Verify round 2 was sent (escalation completed)
        fanout_state = database.fanout_states_db.get(shift_id)
        assert fanout_state.round_2_sent is True
        
        # Verify the sleep was called with 600 seconds (10 minutes)
        mock_sleep.assert_called_once_with(600)


@pytest.mark.asyncio
async def test_escalation_cancelled_on_accept(
    client: AsyncClient, sample_data, mock_notifier
) -> None:
    """
    Test that escalation is cancelled when shift is claimed early.
    """
    shift_id = "f5a9d844-ecff-4f7a-8ef7-d091f22ad77e"

    # Trigger fanout
    await client.post(f"/shifts/{shift_id}/fanout")

    # Verify escalation task exists
    task = database.get_escalation_task(shift_id)
    assert task is not None
    assert not task.done()

    # Accept shift before 10 minutes
    message = {
        "from_phone": "+15550001",  # Alice
        "to_phone": "+15550000",
        "body": "yes",
    }
    response = await client.post("/messages/inbound", json=message)
    assert response.status_code == 200

    # Verify escalation task was cancelled
    task = database.get_escalation_task(shift_id)
    assert task is None or task.cancelled() or task.done()

    # Verify round 2 was NOT sent
    fanout_state = database.fanout_states_db.get(shift_id)
    assert fanout_state.round_2_sent is False


@pytest.mark.asyncio
async def test_race_condition_multiple_accepts(
    client: AsyncClient, sample_data, mock_notifier
) -> None:
    """
    Test that when multiple caregivers accept simultaneously, only the first one gets the shift.
    """
    shift_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"  # LPN shift (contacts Wei and Barry)

    # Trigger fanout (contacts both Wei and Barry)
    await client.post(f"/shifts/{shift_id}/fanout")

    # Both caregivers try to accept simultaneously
    async def accept_shift(phone: str, name: str) -> dict:
        """Helper to send accept message from a caregiver."""
        message = {
            "from_phone": phone,
            "to_phone": "+15550000",
            "body": "yes",
        }
        response = await client.post("/messages/inbound", json=message)
        return {
            "phone": phone,
            "name": name,
            "status_code": response.status_code,
            "data": response.json(),
        }

    # Send both accepts concurrently using gather
    results = await asyncio.gather(
        accept_shift("+15550002", "Wei"),  # Wei Yan
        accept_shift("+15550003", "Barry"),  # Barry Kozumikov
        return_exceptions=False,
    )

    # Verify exactly one succeeded
    success_results = [r for r in results if r["data"].get("status") == "success"]
    assert len(success_results) == 1, f"Expected 1 success, got {len(success_results)}. Results: {results}"

    # The other one should either get "already_claimed" (if it found the shift but it was claimed)
    # or "no_action" (if the shift was already claimed when it checked for pending shifts)
    other_results = [r for r in results if r["data"].get("status") != "success"]
    assert len(other_results) == 1, f"Expected 1 other result, got {len(other_results)}. Results: {results}"
    
    other_status = other_results[0]["data"].get("status")
    assert other_status in [
        "already_claimed",
        "no_action",
    ], f"Expected 'already_claimed' or 'no_action', got '{other_status}'. Results: {results}"

    # Verify shift is claimed
    shift = database.shifts_db.get(shift_id)
    assert shift.status == models.ShiftStatus.CLAIMED
    assert shift.claimed_by in [
        "b7e6a0f4-4c32-44dd-8a6d-ec6b7e9477da",  # Wei
        "c3d4e5f6-g7h8-9012-cdef-345678901234",  # Barry
    ]

    # Verify the successful one is the one who actually got the shift
    successful_caregiver = success_results[0]
    if successful_caregiver["phone"] == "+15550002":  # Wei
        assert shift.claimed_by == "b7e6a0f4-4c32-44dd-8a6d-ec6b7e9477da"
    else:  # Barry
        assert shift.claimed_by == "c3d4e5f6-g7h8-9012-cdef-345678901234"
