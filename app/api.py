import asyncio
import logging

from fastapi import APIRouter, FastAPI, HTTPException

from app import database, intent as intent_parser, models, notifier

router = APIRouter()

logger = logging.getLogger(__name__)


async def _escalate_shift(shift_id: str, caregiver_ids: list[str]) -> None:
    """
    Escalation task: sends phone calls to caregivers if shift is still pending.
    This runs 10 minutes after the initial SMS round.
    """
    try:
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

        # Validate caregiver_ids is not empty
        if not caregiver_ids:
            logger.warning(
                f"No caregiver IDs provided for escalation of shift {shift_id}"
            )
            return

        # Send phone calls to all contacted caregivers
        logger.info(
            f"Escalating shift {shift_id} with phone calls to "
            f"{len(caregiver_ids)} caregivers"
        )
        caregivers = []
        for cg_id in caregiver_ids:
            caregiver = database.caregivers_db.get(cg_id)
            if caregiver:
                caregivers.append(caregiver)
            else:
                logger.warning(
                    f"Caregiver {cg_id} not found during escalation for shift "
                    f"{shift_id}"
                )

        if not caregivers:
            logger.warning(
                f"No valid caregivers found for escalation of shift {shift_id}"
            )
            return

        message = (
            f"Urgent: Shift {shift_id} still needs to be filled. "
            "Please respond if you can accept."
        )

        successful_calls = 0
        failed_calls = 0
        for caregiver in caregivers:
            try:
                await notifier.place_phone_call(caregiver.phone, message)
                successful_calls += 1
            except Exception as e:
                failed_calls += 1
                logger.error(
                    f"Failed to place call to {caregiver.phone} "
                    f"({caregiver.id}) for shift {shift_id}: {e}",
                    exc_info=True,
                )

        logger.info(
            f"Escalation completed for shift {shift_id}: "
            f"{successful_calls} successful, {failed_calls} failed calls"
        )

        # Update fanout state
        fanout_state.round_2_sent = True
        database.fanout_states_db.put(shift_id, fanout_state)
    except Exception as e:
        logger.error(
            f"Unexpected error in escalation for shift {shift_id}: {e}",
            exc_info=True,
        )


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

    successful_sms = 0
    failed_sms = 0
    for caregiver in qualifying_caregivers:
        try:
            await notifier.send_sms(caregiver.phone, message)
            successful_sms += 1
        except Exception as e:
            failed_sms += 1
            logger.error(
                f"Failed to send SMS to {caregiver.phone} "
                f"({caregiver.id}) for shift {shift_id}: {e}",
                exc_info=True,
            )

    if successful_sms == 0:
        logger.error(
            f"Failed to send SMS to any caregivers for shift {shift_id}. "
            "Aborting fanout."
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to send notifications to any caregivers",
        )

    logger.info(
        f"SMS sending completed for shift {shift_id}: "
        f"{successful_sms} successful, {failed_sms} failed"
    )

    # Create fanout state
    try:
        fanout_state = models.FanoutState(
            shift_id=shift_id,
            round_1_sent=True,
            contacted_caregiver_ids=set(caregiver_ids),
        )
        database.fanout_states_db.put(shift_id, fanout_state)
    except Exception as e:
        logger.error(
            f"Failed to create fanout state for shift {shift_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=500, detail="Failed to create fanout state"
        ) from e

    # Schedule escalation task (10 minutes = 600 seconds)
    try:
        escalation_task = asyncio.create_task(
            _escalate_shift_after_delay(shift_id, caregiver_ids)
        )
        database.set_escalation_task(shift_id, escalation_task)
    except Exception as e:
        logger.error(
            f"Failed to schedule escalation task for shift {shift_id}: {e}",
            exc_info=True,
        )
        # Don't fail the request if escalation scheduling fails, but log it
        # The fanout itself was successful

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
    except Exception as e:
        logger.error(
            f"Unexpected error in escalation task for shift {shift_id}: {e}",
            exc_info=True,
        )


@router.post("/messages/inbound")
async def handle_inbound_message(message: models.InboundMessage) -> dict[str, str]:
    """
    Handle an incoming SMS or phone message from a caregiver.

    Processes ACCEPT intents to claim shifts, with race condition protection.
    Ignores DECLINE and UNKNOWN intents.
    """
    # Find caregiver by phone number
    caregiver = None
    for cg in database.caregivers_db.all():
        if cg.phone == message.from_phone:
            caregiver = cg
            break

    if not caregiver:
        logger.warning(
            f"Received message from unknown phone number: {message.from_phone}"
        )
        raise HTTPException(
            status_code=404,
            detail=f"Caregiver with phone number {message.from_phone} not found",
        )

    # Parse message intent
    try:
        intent = await intent_parser.parse_shift_request_message_intent(message.body)
    except Exception as e:
        logger.error(f"Error parsing message intent: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to parse message intent"
        ) from e

    # Only process ACCEPT intents
    if intent != intent_parser.ShiftRequestMessageIntent.ACCEPT:
        logger.info(
            f"Message from {caregiver.name} ({message.from_phone}) has intent "
            f"{intent}, ignoring"
        )
        return {
            "status": "acknowledged",
            "message": "Message received, no action taken",
        }

    # Find shifts where this caregiver was contacted and shift is still pending
    pending_shifts = []
    for fanout_state in database.fanout_states_db.all():
        if caregiver.id in fanout_state.contacted_caregiver_ids:
            shift = database.shifts_db.get(fanout_state.shift_id)
            if shift and shift.status == models.ShiftStatus.PENDING:
                pending_shifts.append((shift, fanout_state))

    if not pending_shifts:
        logger.info(
            f"Caregiver {caregiver.name} ({caregiver.id}) tried to accept, "
            "but no pending shifts found where they were contacted"
        )
        return {
            "status": "no_action",
            "message": "No pending shifts available for you to claim",
        }

    # If multiple shifts, claim the most recent one (by fanout creation time)
    # In practice, you might want to let the caregiver specify which shift
    pending_shifts.sort(
        key=lambda x: x[1].created_at, reverse=True
    )  # Most recent first
    shift, fanout_state = pending_shifts[0]

    # Acquire lock and atomically claim the shift
    try:
        shift_lock = await database.get_shift_lock(shift.id)
    except Exception as e:
        logger.error(
            f"Failed to acquire lock for shift {shift.id}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Failed to process shift claim"
        ) from e

    async with shift_lock:
        try:
            # Re-check shift status after acquiring lock (double-check pattern)
            current_shift = database.shifts_db.get(shift.id)
            if not current_shift:
                logger.warning(f"Shift {shift.id} not found during claim attempt")
                raise HTTPException(
                    status_code=404, detail=f"Shift {shift.id} not found"
                )

            if current_shift.status != models.ShiftStatus.PENDING:
                logger.info(
                    f"Shift {shift.id} is no longer pending "
                    f"(status: {current_shift.status}), cannot be claimed"
                )
                return {
                    "status": "already_claimed",
                    "message": (
                        "This shift has already been claimed by another caregiver"
                    ),
                }

            # Claim the shift
            current_shift.status = models.ShiftStatus.CLAIMED
            current_shift.claimed_by = caregiver.id
            try:
                database.shifts_db.put(shift.id, current_shift)
            except Exception as e:
                logger.error(
                    f"Failed to update shift {shift.id} in database: {e}",
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500, detail="Failed to claim shift"
                ) from e

            # Cancel escalation task if it exists
            try:
                cancelled = database.cancel_escalation_task(shift.id)
                if cancelled:
                    logger.info(f"Cancelled escalation task for shift {shift.id}")
            except Exception as e:
                # Log but don't fail - the shift is already claimed
                logger.warning(
                    f"Failed to cancel escalation task for shift {shift.id}: {e}"
                )

            logger.info(
                f"Shift {shift.id} claimed by caregiver {caregiver.name} "
                f"({caregiver.id})"
            )

            return {
                "status": "success",
                "message": f"Shift {shift.id} has been successfully claimed",
            }
        except HTTPException:
            # Re-raise HTTP exceptions
            raise
        except Exception as e:
            logger.error(
                f"Unexpected error claiming shift {shift.id} for caregiver "
                f"{caregiver.id}: {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail="An unexpected error occurred"
            ) from e


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app
