"""
Domain models for the shift fanout service.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ShiftStatus(str, Enum):
    """Status of a shift."""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"


class Shift(BaseModel):
    """Represents a caregiver shift that needs to be filled."""

    id: str
    organization_id: str
    role_required: str
    start_time: datetime
    end_time: datetime
    status: ShiftStatus = ShiftStatus.PENDING
    claimed_by: str | None = None


class Caregiver(BaseModel):
    """Represents a caregiver who can fill shifts."""

    id: str
    name: str
    role: str
    phone: str


class FanoutState(BaseModel):
    """Tracks the state of fanout for a shift."""

    shift_id: str
    round_1_sent: bool = False
    round_2_sent: bool = False
    contacted_caregiver_ids: set[str] = Field(default_factory=set)
    created_at: datetime = Field(default_factory=lambda: datetime.now())


class InboundMessage(BaseModel):
    """Represents an incoming SMS or phone message."""

    from_phone: str
    to_phone: str
    body: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now())
