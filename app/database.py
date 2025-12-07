import asyncio
from collections.abc import Iterator, MutableMapping
from typing import TypeVar

from app.models import Caregiver, FanoutState, Shift

K = TypeVar("K")
V = TypeVar("V")


class InMemoryKeyValueDatabase[K, V]:
    """
    Simple in-memory key/value database.
    """

    def __init__(self) -> None:
        self._store: MutableMapping[K, V] = {}

    def put(self, key: K, value: V) -> None:
        self._store[key] = value

    def get(self, key: K) -> V | None:
        return self._store.get(key)

    def delete(self, key: K) -> None:
        self._store.pop(key, None)

    def all(self) -> list[V]:
        return list(self._store.values())

    def clear(self) -> None:
        self._store.clear()

    def __iter__(self) -> Iterator[V]:
        return iter(self._store.values())

    def __len__(self) -> int:
        return len(self._store)


# Typed database instances
shifts_db: InMemoryKeyValueDatabase[str, Shift] = (
    InMemoryKeyValueDatabase[str, Shift]()
)
caregivers_db: InMemoryKeyValueDatabase[str, Caregiver] = (
    InMemoryKeyValueDatabase[str, Caregiver]()
)
fanout_states_db: InMemoryKeyValueDatabase[str, FanoutState] = (
    InMemoryKeyValueDatabase[str, FanoutState]()
)

# Per-shift locks for race condition protection
_shift_locks: dict[str, asyncio.Lock] = {}
_locks_lock = asyncio.Lock()

# Escalation task tracking (shift_id -> asyncio.Task)
_escalation_tasks: dict[str, asyncio.Task] = {}


async def get_shift_lock(shift_id: str) -> asyncio.Lock:
    """
    Get or create an asyncio.Lock for a specific shift.
    Thread-safe lock creation.
    """
    async with _locks_lock:
        if shift_id not in _shift_locks:
            _shift_locks[shift_id] = asyncio.Lock()
        return _shift_locks[shift_id]


def set_escalation_task(shift_id: str, task: asyncio.Task) -> None:
    """Store an escalation task for a shift."""
    _escalation_tasks[shift_id] = task


def get_escalation_task(shift_id: str) -> asyncio.Task | None:
    """Get the escalation task for a shift, if it exists."""
    return _escalation_tasks.get(shift_id)


def cancel_escalation_task(shift_id: str) -> bool:
    """
    Cancel the escalation task for a shift if it exists and hasn't completed.
    Returns True if a task was found and cancelled, False otherwise.
    """
    task = _escalation_tasks.get(shift_id)
    if task and not task.done():
        task.cancel()
        _escalation_tasks.pop(shift_id, None)
        return True
    return False
