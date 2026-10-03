# events — In-process async pub/sub event bus for decoupled cross-domain side effects

> This module provides a simple in-process publish/subscribe event bus that enables domains to react to events from other domains without creating direct dependencies. It solves the problem of tight coupling in a multi-domain architecture by allowing services to emit events that other services subscribe to, enabling side effects like notifications or group membership updates to trigger from domain events without those domains knowing about each other.

**Categories:** Infrastructure/Foundation, Event-Driven Architecture, Cross-Domain Communication, Async/Concurrency Patterns  
**Concepts:** EventBus, event-driven architecture, pub/sub pattern, publish/subscribe, async/await, decoupling, cross-domain side effects, handler registration, exception isolation, sequential execution  
**Words:** 1587 | **Version:** 1

---

## Purpose

The `events` module exists to solve a fundamental architectural problem: **how do you trigger side effects across domains without creating tight coupling?**

In a multi-domain system (invite domain, notification domain, group domain, etc.), you often need actions in one domain to trigger reactions in another. For example, when an invite is accepted, you might need to:
- Create a notification
- Auto-add the user to a group
- Update analytics
- Send a webhook

Without an event bus, the invite domain would need to import and directly call functions from the notification, group, and analytics domains. This creates a tangled dependency graph where every domain knows about every other domain.

The `EventBus` solves this by providing a **pub/sub (publish/subscribe) contract**: domains emit events without knowing who cares about them, and other domains subscribe to those events without knowing where they come from. This is a classic decoupling pattern used in event-driven architectures.

## Key Classes and Methods

### EventBus

The core class that manages all subscriptions and emissions.

**`__init__()`**
Initializes an empty event bus with a `defaultdict` that maps event names (strings) to lists of handler functions. Using `defaultdict(list)` is a design choice that eliminates the need to check if an event key exists — accessing a missing event automatically creates an empty list.

**`subscribe(event: str, handler: Handler) -> None`**
Registers a handler function to be called whenever an event is emitted. The same handler can be registered multiple times for the same event (it will be called multiple times). The handler is appended to a list in subscription order, meaning handlers are executed in the order they were registered. This is critical for predictable side effect sequencing.

**`unsubscribe(event: str, handler: Handler) -> None`**
Removes a specific handler from an event's subscription list. Uses a try/except pattern to silently ignore attempts to unsubscribe handlers that were never registered ("no-op if not subscribed"). This is defensive programming — it prevents errors in cleanup code.

**`async def emit(event: str, data: dict[str, Any]) -> None`**
The core async method that triggers all subscribed handlers for a given event. This is where the actual side effects happen. Key characteristics:
- Calls handlers **sequentially** in subscription order (not concurrently), so handlers run one after another
- **Exception safety**: if one handler raises an exception, it's logged but remaining handlers still execute (isolation between handlers)
- Uses `logger.exception()` to capture the full stack trace for debugging
- Safely gets the handler's name using `getattr(handler, "__name__", handler)` to handle lambdas or callable objects

### Handler Type Alias

```python
Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]
```

This type hint is critical for understanding the contract: handlers are async functions that accept a data dictionary and return nothing. They're coroutines that must be awaited.

## How It Works

### Data Flow

1. **Subscription Phase** (happens at module/application startup, or during configuration):
   - A service imports `event_bus` and calls `event_bus.subscribe("invite.accepted", my_handler)`
   - The handler function is stored in `_handlers["invite.accepted"]`

2. **Emission Phase** (happens when a domain action completes):
   - A domain emits an event: `await event_bus.emit("invite.accepted", {"user_id": 123, ...})`
   - The event bus looks up all handlers in `_handlers["invite.accepted"]`
   - For each handler, it awaits the coroutine, passing the data dictionary

3. **Side Effects Execution**:
   - Each handler runs sequentially and can perform async operations (database writes, API calls, etc.)
   - If any handler fails, it's logged but doesn't block other handlers

### Control Flow Example

```
Invite Domain:
  await event_bus.emit("invite.accepted", {"user_id": 123, "group_id": 456})
  ↓
EventBus.emit() looks up handlers for "invite.accepted"
  ↓
Notification Service handler runs: creates notification
  ↓
Group Service handler runs: adds user to group
  ↓
Analytics Service handler runs: logs event
  ↓
All handlers complete (or fail safely with logging)
  ↓
Invite domain continues (emitter doesn't wait or care about results)
```

### Important Edge Cases

1. **No handlers registered**: If you emit an event with no subscribers, `self._handlers[event]` creates an empty list via `defaultdict`, and the loop simply doesn't execute. No error.

2. **Handler raises exception**: The exception is caught, logged with full traceback, and execution continues to the next handler. This prevents one broken subscriber from breaking all subscribers.

3. **Emitting from a handler**: A handler can call `event_bus.emit()` again, potentially creating a chain of events. However, this is synchronous ordering — the original emit() call will await all nested emissions.

4. **Concurrent emissions**: If multiple coroutines call `emit()` at the same time, they run concurrently in the event loop. However, within a single `emit()` call, handlers run sequentially.

5. **Order matters**: Handlers execute in subscription order. If handler A calls something that is read by handler B, handler A must be subscribed first.

## Authorization and Security

This module **has no built-in authorization or security**. It's an in-process mechanism used by trusted internal code (the service layer). Key considerations:

- **No event validation**: The data dict is passed as-is to handlers. There's no schema validation, type checking, or ACL enforcement.
- **No authentication**: Any code running in the same process can subscribe or emit any event.
- **Information leakage risk**: Event data contains raw domain information. If a handler is compromised or misconfigured, it could access data it shouldn't.

**Security responsibility** belongs to the callers: each domain should only emit events with appropriate data, and handlers should only subscribe to events they should process. This is a **convention-based security model**.

## Dependencies and Integration

### What This Module Depends On

- **Python standard library only**: `logging`, `collections`, `collections.abc`, `typing`
- No external packages or database access
- This is intentional — the event bus is a lightweight infrastructure component

### What Depends on This Module

Based on the import graph, **four services depend on `events`**:

1. **message_service**: Likely subscribes to events like "user.created" or "group.updated" to trigger message-related side effects
2. **service**: A core service module that orchestrates domain logic and probably emits domain events
3. **agent_bridge**: Likely subscribes to events to send information to external agents or webhooks
4. **event_handlers**: A dedicated module (possibly in `handler_registry.py` or similar) that registers all event subscriptions during application startup

### Typical Integration Pattern

```
Domain Layer (e.g., invite_service):
  - Performs core domain logic
  - Calls: await event_bus.emit("invite.accepted", {...})

Event Handlers Layer (handler registration):
  - Subscribes notification_handler to "invite.accepted"
  - Subscribes group_handler to "invite.accepted"
  - Subscribes analytics_handler to "invite.accepted"

Message/Notification Layer:
  - Async handler that creates notifications on event

Group Layer:
  - Async handler that manages group membership on event
```

This creates a **clean dependency graph** where the core domain doesn't know about side effects.

## Design Decisions

### 1. **Sequential Handler Execution (Not Concurrent)**
Handlers are awaited sequentially with `await handler(data)` inside a for loop. This means:
- **Pro**: Predictable ordering, easier debugging, no race conditions between handlers
- **Con**: If one handler is slow, all handlers after it are blocked
- **Reasoning**: For side effects, ordering and consistency matter more than latency. If you need true concurrency, you can use `asyncio.gather()` in the calling code.

### 2. **Graceful Exception Handling**
Exceptions in handlers are logged but don't stop other handlers. This prevents cascading failures:
- **Pro**: Resilience — one broken handler doesn't break all subscribers
- **Con**: Silent failures — exceptions are logged but not raised to the caller, so the emitter doesn't know if side effects failed
- **Reasoning**: Event handlers are often "fire and forget" side effects. The original action (e.g., accept invite) shouldn't fail because a notification failed to send.

### 3. **Module-Level Singleton**
```python
event_bus = EventBus()
```
A single global instance is created and imported throughout the codebase. This ensures:
- **Pro**: Simple API, no DI container needed, consistent subscriptions across the app
- **Con**: Global state, harder to test in isolation, tightly couples to this module
- **Reasoning**: This is an infrastructure component that's meant to be a shared utility. The entire app uses one event bus.

### 4. **Type Alias for Handlers**
The `Handler` type is explicit: `Callable[[dict[str, Any]], Coroutine[Any, Any, None]]`. This:
- **Pro**: Clear contract, IDE autocomplete works, type checkers enforce the signature
- **Con**: Uses `Any` heavily, doesn't capture semantic meaning of data dict
- **Reasoning**: Without schema libraries like Pydantic, `dict[str, Any]` is the practical choice. Event data is loosely typed by design to avoid coupling domains.

### 5. **defaultdict vs Regular dict**
Using `defaultdict(list)` instead of regular `dict`:
- **Pro**: No KeyError if you emit an event with no handlers
- **Con**: Less explicit — you can't tell if an event name is misspelled
- **Reasoning**: Convenience over explicitness. Emitting to nobody is a valid scenario (maybe some deployments don't have all handlers).

### 6. **In-Process Only (Not Distributed)**
This is a single-process pub/sub, not a message broker:
- **Pro**: No network latency, no distributed system complexity, no external dependencies
- **Con**: Only works within one process, no cross-service events, lost on process restart
- **Reasoning**: This is for **internal side effects within the cloud service**. Cross-service communication would use message brokers (RabbitMQ, Kafka, etc.), which is out of scope here.

## Common Patterns and Usage

### Registering Handlers (Typically in handler_registry or event_handlers module)
```python
from ee.cloud.shared.events import event_bus
from notification_service import create_notification
from group_service import add_user_to_group

async def on_invite_accepted(data: dict[str, Any]) -> None:
    await create_notification(data["user_id"], "Your invite was accepted!")

async def on_invite_accepted_group(data: dict[str, Any]) -> None:
    await add_user_to_group(data["user_id"], data["group_id"])

event_bus.subscribe("invite.accepted", on_invite_accepted)
event_bus.subscribe("invite.accepted", on_invite_accepted_group)
```

### Emitting Events (From domain services)
```python
from ee.cloud.shared.events import event_bus

async def accept_invite(invite_id: str):
    invite = await Invite.get(invite_id)
    invite.status = "accepted"
    await invite.save()
    
    # Trigger side effects
    await event_bus.emit("invite.accepted", {
        "invite_id": invite_id,
        "user_id": invite.user_id,
        "group_id": invite.group_id,
    })
```

---

## Related

- [untitled](untitled.md)
