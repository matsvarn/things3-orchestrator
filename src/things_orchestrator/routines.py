"""Single-loop scheduler for the optional routines projection and delivery."""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Literal, Protocol, TypeVar

import anyio

from .cloud import HistoryBatch, HistoryIdentityChanged
from .routines_config import (
    EnabledRoutineConfig,
    RoutineProfile,
    account_digest,
    load_routines_config,
)
from .routines_store import (
    RoutineHistoryIdentityChanged,
    StoredEvent,
)
from .routines_webhook import DeliveryResult

RuntimeState = Literal["disabled", "initializing", "running", "backing_off", "stopped"]
T = TypeVar("T")
WaitForStop = Callable[[float, anyio.Event], Awaitable[None]]


async def _wait_for_stop(delay: float, stop: anyio.Event) -> None:
    with anyio.move_on_after(delay):
        await stop.wait()


class GroupedHistoryClient(Protocol):
    def history_groups(self, start_index: int) -> HistoryBatch: ...


class RoutineStoreProtocol(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    def cursor(self) -> int: ...

    def apply_batch(self, batch: HistoryBatch, *, observed_at: int) -> None: ...

    def reset_history(self) -> None: ...

    def due_events(
        self, *, now: int, limit: int = 25
    ) -> tuple[StoredEvent, ...]: ...

    def record_attempt(
        self,
        event_id: str,
        *,
        attempted_at: int,
        state: str,
        next_attempt_at: int | None,
        http_status: int | None,
        result: str,
    ) -> None: ...


class RoutineWebhookProtocol(Protocol):
    def deliver(
        self, event: StoredEvent, *, timestamp: int
    ) -> DeliveryResult: ...


class _RoutineDisabled(Exception):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    state: RuntimeState
    cloud_failures: int = 0
    delivery_failures: int = 0
    last_successful_poll_at: int | None = None
    last_delivery_at: int | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "state": self.state,
            "cloud_failures": self.cloud_failures,
            "delivery_failures": self.delivery_failures,
        }
        if self.last_successful_poll_at is not None:
            result["last_successful_poll_at"] = self.last_successful_poll_at
        if self.last_delivery_at is not None:
            result["last_delivery_at"] = self.last_delivery_at
        return result


def _active_snapshot(
    cloud_failures: int,
    delivery_failures: int,
    *,
    last_successful_poll_at: int | None = None,
    last_delivery_at: int | None = None,
) -> RuntimeSnapshot:
    state: Literal["running", "backing_off"] = (
        "backing_off" if cloud_failures or delivery_failures else "running"
    )
    return RuntimeSnapshot(
        state,
        cloud_failures,
        delivery_failures,
        last_successful_poll_at,
        last_delivery_at,
    )


class RoutineWorker:
    def __init__(
        self,
        *,
        email: str,
        profile: RoutineProfile,
        cloud: GroupedHistoryClient,
        store: RoutineStoreProtocol,
        webhook: RoutineWebhookProtocol,
        epoch: Callable[[], int] = lambda: int(time.time()),
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[float], float] = lambda upper: random.uniform(0, upper),
        config_loader: Callable[[], object] = load_routines_config,
        wait_for_stop: WaitForStop = _wait_for_stop,
    ) -> None:
        self._email = email
        self._profile = profile
        self._cloud = cloud
        self._store = store
        self._webhook = webhook
        self._epoch = epoch
        self._monotonic = monotonic
        self._jitter = jitter
        self._config_loader = config_loader
        self._wait_for_stop = wait_for_stop
        self._limiter = anyio.CapacityLimiter(1)
        self._last_successful_poll_at: int | None = None
        self._last_delivery_at: int | None = None
        self._snapshot = self._snapshot_for("initializing")

    def snapshot(self) -> dict[str, object]:
        return self._snapshot.as_dict()

    async def run(self, stop: anyio.Event) -> None:
        cloud_failures = 0
        delivery_failures = 0
        try:
            await self._sync(self._store.open)
        except Exception:
            self._snapshot = self._snapshot_for("stopped")
            return
        next_poll = self._monotonic()
        next_delivery = self._monotonic()
        next_config = self._monotonic() + self._profile.poll_interval_seconds
        self._snapshot = self._active_snapshot(cloud_failures, delivery_failures)
        try:
            while not stop.is_set():
                now_mono = self._monotonic()
                if now_mono >= next_config:
                    if not await self._still_enabled():
                        self._snapshot = self._snapshot_for(
                            "disabled", cloud_failures, delivery_failures
                        )
                        return
                    next_config = now_mono + self._profile.poll_interval_seconds

                if now_mono >= next_poll:
                    try:
                        caught_up = await self._poll_once()
                    except (HistoryIdentityChanged, RoutineHistoryIdentityChanged):
                        try:
                            await self._sync(self._store.reset_history)
                        except Exception:
                            cloud_failures += 1
                            next_poll = self._monotonic() + self._cloud_delay(
                                cloud_failures
                            )
                            self._snapshot = self._active_snapshot(
                                cloud_failures, delivery_failures
                            )
                        else:
                            cloud_failures = 0
                            next_poll = self._monotonic()
                            self._snapshot = self._active_snapshot(
                                cloud_failures, delivery_failures
                            )
                    except Exception:
                        cloud_failures += 1
                        next_poll = self._monotonic() + self._cloud_delay(
                            cloud_failures
                        )
                        self._snapshot = self._active_snapshot(
                            cloud_failures, delivery_failures
                        )
                    else:
                        cloud_failures = 0
                        next_poll = self._monotonic() + (
                            self._profile.poll_interval_seconds if caught_up else 0
                        )
                        self._snapshot = self._active_snapshot(
                            cloud_failures, delivery_failures
                        )
                    continue

                if now_mono >= next_delivery:
                    try:
                        attempted, failed = await self._drain_due(
                            stop=stop, poll_due_at=next_poll
                        )
                    except _RoutineDisabled:
                        self._snapshot = self._snapshot_for(
                            "disabled", cloud_failures, delivery_failures
                        )
                        return
                    except Exception:
                        delivery_failures += 1
                        next_delivery = self._monotonic() + self._delivery_delay(
                            delivery_failures
                        )
                        self._snapshot = self._active_snapshot(
                            cloud_failures, delivery_failures
                        )
                    else:
                        delivery_failures = delivery_failures + failed if failed else 0
                        next_delivery = self._monotonic() + (1 if attempted else 5)
                        self._snapshot = self._active_snapshot(
                            cloud_failures, delivery_failures
                        )
                    continue

                delay = max(
                    0.0,
                    min(next_poll, next_delivery, next_config) - self._monotonic(),
                )
                await self._wait_for_stop(delay, stop)
        finally:
            if self._snapshot.state != "disabled":
                self._snapshot = self._snapshot_for(
                    "stopped", cloud_failures, delivery_failures
                )
            try:
                await self._sync(self._store.close)
            except Exception:
                pass

    async def _poll_once(self) -> bool:
        cursor = await self._sync(self._store.cursor)
        batch = await self._sync(partial(self._cloud.history_groups, cursor))
        observed_at = self._epoch()
        await self._sync(
            partial(self._store.apply_batch, batch, observed_at=observed_at)
        )
        self._last_successful_poll_at = observed_at
        return batch.caught_up

    async def _drain_due(
        self, *, stop: anyio.Event, poll_due_at: float
    ) -> tuple[int, int]:
        due = await self._sync(partial(self._store.due_events, now=self._epoch()))
        attempted = 0
        failures = 0
        for event in due:
            if stop.is_set() or self._monotonic() >= poll_due_at:
                break
            if not await self._still_enabled():
                raise _RoutineDisabled
            result = await self._sync(
                partial(self._webhook.deliver, event, timestamp=self._epoch())
            )
            await self._record_delivery(event, result)
            attempted += 1
            failures += result.kind != "delivered"
        return attempted, failures

    async def _record_delivery(
        self, event: StoredEvent, result: DeliveryResult
    ) -> None:
        now = self._epoch()
        attempts = event.attempt_count + 1
        exhausted = (
            attempts >= self._profile.retry.max_attempts
            or now - event.observed_at >= self._profile.retry.max_age_seconds
        )
        if result.kind == "delivered":
            state = "delivered"
            next_attempt = None
        elif result.kind == "permanent" or exhausted:
            state = "dead"
            next_attempt = None
        else:
            state = "pending"
            ceiling = min(
                self._profile.retry.max_delay_seconds,
                self._profile.retry.initial_delay_seconds
                * (2 ** min(event.attempt_count, 30)),
            )
            delay = int(self._jitter(float(ceiling)))
            if result.retry_after_seconds is not None:
                delay = max(delay, min(result.retry_after_seconds, ceiling))
            next_attempt = now + max(delay, 1)
        await self._sync(
            partial(
                self._store.record_attempt,
                event.event_id,
                attempted_at=now,
                state=state,
                next_attempt_at=next_attempt,
                http_status=result.http_status,
                result=result.code,
            )
        )
        if state == "delivered":
            self._last_delivery_at = now

    async def _still_enabled(self) -> bool:
        try:
            config = await self._sync(self._config_loader)
        except Exception:
            return False
        return (
            isinstance(config, EnabledRoutineConfig)
            and config.profile.account_digest == account_digest(self._email)
            and config.profile == self._profile
        )

    async def _sync(self, function: Callable[[], T]) -> T:
        return await anyio.to_thread.run_sync(function, limiter=self._limiter)

    def _cloud_delay(self, failures: int) -> float:
        ceiling = min(900.0, 5.0 * (2 ** min(max(failures - 1, 0), 30)))
        return max(1.0, self._jitter(ceiling))

    def _delivery_delay(self, failures: int) -> float:
        ceiling = min(
            float(self._profile.retry.max_delay_seconds),
            float(self._profile.retry.initial_delay_seconds)
            * (2 ** min(max(failures - 1, 0), 30)),
        )
        return max(1.0, self._jitter(ceiling))

    def _snapshot_for(
        self,
        state: RuntimeState,
        cloud_failures: int = 0,
        delivery_failures: int = 0,
    ) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            state,
            cloud_failures,
            delivery_failures,
            self._last_successful_poll_at,
            self._last_delivery_at,
        )

    def _active_snapshot(
        self, cloud_failures: int, delivery_failures: int
    ) -> RuntimeSnapshot:
        return _active_snapshot(
            cloud_failures,
            delivery_failures,
            last_successful_poll_at=self._last_successful_poll_at,
            last_delivery_at=self._last_delivery_at,
        )
