import asyncio
import logging

from fastapi import APIRouter, FastAPI, HTTPException

from app import database, models, notifier

router = APIRouter()

logger = logging.getLogger(__name__)


async def _escalate_shift(shift_id: str, caregiver_ids: list[str]) -> None:
    """
    Escalation task: sends phone calls to caregivers if shift is still pending.
    This runs 10 minutes after the initial SMS round.
    """
    # Check if shift is still pending before sending calls
    shift = database.shifts_db.get(shift_id)
    if not shift:
        logger.warning(f"Shift {shift_id} not found during escalation")
        return

    if shift.status != models.ShiftStatus.PENDING:
        logger.info(
            f"Shift {shift_id} is no longer pending (status: {shift.status}), "
            "skipping escalation"
        )
        return

    # Get fanout state
    fanout_state = database.fanout_states_db.get(shift_id)
    if not fanout_state:
        logger.warning(f"Fanout state not found for shift {shift_id}")
        return

    # Send phone calls to all contacted caregivers
    logger.info(f"Escalating shift {shift_id} with phone calls")
    caregivers = [
        database.caregivers_db.get(cg_id)
        for cg_id in caregiver_ids
        if database.caregivers_db.get(cg_id)
    ]

    message = (
        f"Urgent: Shift {shift_id} still needs to be filled. "
        "Please respond if you can accept."
    )

    for caregiver in caregivers:
        try:
            await notifier.place_phone_call(caregiver.phone, message)
        except Exception as e:
            logger.error(
                f"Failed to place call to {caregiver.phone} for shift {shift_id}: {e}"
            )

    # Update fanout state
    fanout_state.round_2_sent = True
    database.fanout_states_db.put(shift_id, fanout_state)


@router.post("/shifts/{shift_id}/fanout")
async def fanout_shift(shift_id: str) -> dict[str, str]:
    """
    Trigger fanout for a shift. Sends SMS to qualifying caregivers immediately,
    and schedules phone calls after 10 minutes if shift is still unfilled.

    Idempotent: re-posting the same shift fanout will not send duplicate
    notifications.
    """
    # Validate shift exists
    shift = database.shifts_db.get(shift_id)
    if not shift:
        raise HTTPException(status_code=404, detail=f"Shift {shift_id} not found")

    # Check if shift is already claimed
    if shift.status == models.ShiftStatus.CLAIMED:
        raise HTTPException(
            status_code=409,
            detail=f"Shift {shift_id} has already been claimed",
        )

    # Check idempotency: if fanout already initiated, return success
    existing_fanout = database.fanout_states_db.get(shift_id)
    if existing_fanout:
        logger.info(
            f"Fanout already initiated for shift {shift_id}, returning success "
            "(idempotent)"
        )
        return {"status": "success", "message": "Fanout already initiated"}

    # Filter caregivers by role_required
    qualifying_caregivers = [
        caregiver
        for caregiver in database.caregivers_db.all()
        if caregiver.role == shift.role_required
    ]

    if not qualifying_caregivers:
        logger.warning(
            f"No caregivers found with role {shift.role_required} for shift "
            f"{shift_id}"
        )
        # Return success even if no caregivers found (per requirements)
        return {
            "status": "success",
            "message": "No qualifying caregivers found",
        }

    # Send SMS to all qualifying caregivers (round 1)
    caregiver_ids = [cg.id for cg in qualifying_caregivers]
    message = (
        f"New shift available: {shift.start_time} to {shift.end_time}. "
        "Reply 'yes' or 'accept' to claim this shift."
    )

    logger.info(
        f"Sending SMS to {len(qualifying_caregivers)} caregivers for shift "
        f"{shift_id}"
    )

    for caregiver in qualifying_caregivers:
        try:
            await notifier.send_sms(caregiver.phone, message)
        except Exception as e:
            logger.error(
                f"Failed to send SMS to {caregiver.phone} for shift {shift_id}: {e}"
            )

    # Create fanout state
    fanout_state = models.FanoutState(
        shift_id=shift_id,
        round_1_sent=True,
        contacted_caregiver_ids=set(caregiver_ids),
    )
    database.fanout_states_db.put(shift_id, fanout_state)

    # Schedule escalation task (10 minutes = 600 seconds)
    escalation_task = asyncio.create_task(
        _escalate_shift_after_delay(shift_id, caregiver_ids)
    )
    database.set_escalation_task(shift_id, escalation_task)

    return {
        "status": "success",
        "message": f"Fanout initiated for shift {shift_id}",
    }


async def _escalate_shift_after_delay(
    shift_id: str, caregiver_ids: list[str]
) -> None:
    """
    Wait 10 minutes, then escalate if shift is still pending.
    """
    try:
        await asyncio.sleep(600)  # 10 minutes
        await _escalate_shift(shift_id, caregiver_ids)
    except asyncio.CancelledError:
        logger.info(f"Escalation task for shift {shift_id} was cancelled")
        raise


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app
