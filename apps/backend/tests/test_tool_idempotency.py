"""Two incidents must never share a tool-call record.

The idempotency key used to be ``sha256(tool_name + canonical args)`` with no
run in it. Every incident in the system asking the same question therefore
produced the same key, so the second incident's call returned the *first*
incident's row: its own ``tool_calls`` table stayed empty and
``finish_tool_call`` rewrote another run's audit record. The evaluation caught
it — 8 of 12 scenarios reported zero tool calls while passing every other
check — which is exactly the class of bug that only surfaces when you run the
thing twice.
"""

from __future__ import annotations

from opspilot_backend.tools.registry import idempotency_key


def test_key_is_stable_for_the_same_invocation() -> None:
    """Replay protection still works: identical inputs, identical key."""
    args = {"service": "payment-service", "limit": 5}
    assert idempotency_key("query_logs", args, scope="run-1", occurrence=3) == (
        idempotency_key("query_logs", args, scope="run-1", occurrence=3)
    )


def test_key_ignores_argument_order() -> None:
    assert idempotency_key(
        "query_metrics", {"service": "a", "metric": "b"}, scope="r", occurrence=1
    ) == idempotency_key(
        "query_metrics", {"metric": "b", "service": "a"}, scope="r", occurrence=1
    )


def test_different_runs_do_not_collide() -> None:
    """The regression: two incidents asking the same question."""
    args = {"service": "payment-service"}
    first = idempotency_key("get_service_status", args, scope="run-A", occurrence=1)
    second = idempotency_key("get_service_status", args, scope="run-B", occurrence=1)
    assert first != second


def test_repeated_calls_inside_one_run_are_distinct() -> None:
    """Calling the same tool twice in a run is two audited calls.

    Collapsing them would hide the second observation — "the pool looked fine
    before the fix and exhausted after" is the whole point of re-querying.
    """
    args = {"service": "checkout-service"}
    assert idempotency_key("get_service_status", args, scope="run-A", occurrence=1) != (
        idempotency_key("get_service_status", args, scope="run-A", occurrence=2)
    )


def test_occurrence_survives_a_resumed_leg() -> None:
    """A post-approval leg must not re-use the first leg's indices.

    ``NodeContext`` seeds its call counter from the run's stored step count,
    so a resumed run keeps climbing instead of restarting at 1.
    """
    args = {"service": "redis"}
    fresh = idempotency_key("restart_redis", args, scope="run-A", occurrence=1)
    resumed = idempotency_key("restart_redis", args, scope="run-A", occurrence=17)
    assert fresh != resumed
