See [README.md](README.md) for instructions.

# Project Reflection

## Project Structure

I leveraged the following structure for this project, based on the boilerplate code:

- **`app/models.py`**: Pydantic models for type safety and validation, flowing throughout the entire system - `Shift`, `Caregiver`, `FanoutState`, and `InboundMessage`.

- **`app/database.py`**: Extended boilerplate code with typed database instances (`shifts_db`, `caregivers_db`, `fanout_states_db`) and manages concurrency primitives (per-shift locks and escalation task tracking).

- **`app/api.py`**: Contains the FastAPI endpoints that orchestrate the fanout workflow and handle inbound messages. This is where the core logic is implemented.

- **`app/notifier.py`** and **`app/intent.py`**: Stubbed external dependencies that would integrate with external APIs and LLMs for intent classification services. This was not touched from my end.

## Model Objects Through the Workflow

This is how the data moves throughout the workflow:

1. **Fanout Initiation**: When `POST /shifts/{shift_id}/fanout` is called, the API service retrieves a `Shift` object via the `shifts_db`, filters `Caregiver` objects by role, and finally creates a `FanoutState` object to track the fanout process. The `FanoutState` stores which caregivers were contacted and tracks the two-round escalation state, which were validated in pytests.

2. **Message Processing**: When `POST /messages/inbound` receives an `InboundMessage`, the corresponding `Caregiver` is located by phone number, then parsed and the intent is classified, and then queries `FanoutState` objects to find pending shifts where that caregiver was contacted. The `Shift` object is then atomically updated through the lock mechanism, handling race conditions given the concurrency of the fanout.

## Fanout and Race Condition Handling

The fanout process and race condition protection work together via the database primitives and API logic:

**Fanout Logic:**
1. The API checks for an existing `FanoutState` to ensure idempotency as required by the guidelines
2. Filters caregivers by querying matching roles via `shift.role_required`
3. Sends the first round SMS notifications and creates `FanoutState` with `contacted_caregiver_ids`
4. Schedules an escalation task that will commence after 10 minutes, which will be the second layer of the fanout

**Race Condition Handling:**
I handled race conditions by using 2 layers:

I implemented per-shift `asyncio.Lock` objects (managed by `database.get_shift_lock()`) so that only one caregiver can claim a shift at a time. Inside the lock's key logic, I included code that re-verifies the shift status before claiming it, handling cases where multiple requests find the shift as pending but one has already claimed it by the time they acquire the lock. All status changes and database updates happen atomically within this locked section.

The `database.py` module manages the lock lifecycle by creating locks upon caregiver requests in a thread-safe manner using a meta-lock (`_locks_lock`), this handles race conditions when creating locks.

## Escalation Task Management

Escalation tasks are tracked separately in `database.py` using `_escalation_tasks` dictionary. When a shift is claimed early, the escalation task is cancelled to prevent unnecessary phone calls. The task reference is stored when created and can be cancelled if the shift is claimed before the 10-minute delay completes.

## Testing

1. **Setup**: I utilized the given `sample_data.json` data, loading it before each test and cleaning up after, ensuring test isolation.

2. **Mocking**: The `mock_notifier` mocks the notifier functions to make tests run faster (the real functions have 1-2 second delays from the stubbed notifier). For escalation tests, `asyncio.sleep` is mocked to allow escalation logic to execute without waiting the full 10 minutes.

3. **Coverage**: Tests cover:
   - Basic functionality (fanout, acceptance)
   - Error cases (404s, already claimed)
   - Idempotency
   - Role filtering
   - Race conditions (simultaneous accepts)
   - Escalation logic (with mocked time)
   - Escalation cancellation

## Design Decisions

**1. Per-Shift Locks vs Global Lock**: I chose per-shift locks rather than a single global lock to allow concurrent processing of different shifts. This provides better performance when multiple shifts are being processed simultaneously, which is key for the function of this project.

**2. FanoutState as Idempotency Mechanism**: The `FanoutState` object serves dual purposes - tracking fanout progress AND ensuring idempotency. If a `FanoutState` exists for a shift, then we know fanout was already initiated and can return early without sending duplicate notifications.

**3. Escalation Task Storage**: Escalation tasks are stored in `database.py` rather than in the `FanoutState` model because Pydantic models can't directly store `asyncio.Task` objects since they can't be serialized and are live runtime objects. This separation keeps the model clean while allowing task cancellation.

**4. Double-Check Pattern**: After acquiring the lock, the code re-checks the shift status. This is necessary because between finding the shift in `pending_shifts` and acquiring the lock, another request might have claimed the shift. This approach helps to ensure robust handling of high concurrency.

## LLM Dev Process

I used LLMs throughout this project to quickly test design ideas and maintain flexibility in the development process. When planning my implementation, I leveraged Cursor to quickly explore different approaches to key implementation decisions such as race condition handling and state management. This allowed me to quickly scope out my design decisions before committing to a single one. Another pain point where I leveraged LLMs was when I ran into errors with my code, LLM tools were helpful to quickly identify relevant areas in the codebase and ensure fast refinement of buggy lines of code or design issues. LLM tools were also helpful to act as a reviewer for my code, allowing me to create more robust testing and error handling throughout my code. My main perspective throughout this project was to use LLMs as a partner to increase flexibility and efficiency throughout my implementation process.