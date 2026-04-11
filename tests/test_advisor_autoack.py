"""Regression tests for the advisor auto-ack chain (task #129732).

Closes the self-feeding loop so workers don't need Alex to manually
tell the advisor "terminal X is done". The chain has four hops:

    /done (worker)           → wire task_complete ack to advisor
    /advisor-react (cron)    → poll unread acks, fire /advisor-plan per sender
    /advisor-plan (advisor)  → pick enriched next task, wire task_handoff
    /dispatch-next (worker)  → Step 1b polls for task_handoff, claims it

All four pieces exist today — this test pins their invariants so a
future skill edit can't silently sever the loop. It does NOT simulate
end-to-end delivery (that would require a live advisor + cron); it
verifies the *code paths* each stage relies on.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


SKILLS = Path.home() / ".claude" / "skills"
BATTLESTATION_WIRE = Path.home() / "battlestation" / "lib" / "wire.sh"

DONE = SKILLS / "done" / "SKILL.md"
ADVISOR_REACT = SKILLS / "advisor-react" / "SKILL.md"
ADVISOR_PLAN = SKILLS / "advisor-plan" / "SKILL.md"
DISPATCH_NEXT = SKILLS / "dispatch-next" / "SKILL.md"


def _skip_if_missing(path: Path):
    if not path.exists():
        pytest.skip(f"skill file missing: {path}")


# ---------------------------------------------------------------------------
# /done — the worker-side auto-ack
# ---------------------------------------------------------------------------


class TestDoneAutoAck:
    def test_done_skill_exists(self):
        _skip_if_missing(DONE)
        assert DONE.exists()

    def test_done_calls_wire_send_with_task_complete(self):
        """/done must wire a task_complete status to the advisor."""
        _skip_if_missing(DONE)
        body = DONE.read_text()
        assert "wire_send" in body, "/done does not source wire_send"
        assert "task_complete" in body, (
            "/done must emit 'task_complete' — it's the regex the advisor matches"
        )
        # The advisor discovery query looks for role=eq.advisor
        assert "role=eq.advisor" in body, (
            "/done does not discover the advisor via session_locks"
        )

    def test_done_auto_ack_is_unconditional(self):
        """The auto-ack step must fire even with no active session_task.

        Prior versions gated the ack behind ``if [ -n "$task_id" ]`` which
        meant ad-hoc work never acked. Step 3f explicitly removes that gate.
        """
        _skip_if_missing(DONE)
        body = DONE.read_text()
        assert "runs UNCONDITIONALLY" in body or "fires EVEN IF" in body, (
            "/done auto-ack appears to be gated on an active task — it must fire unconditionally"
        )

    def test_done_chains_to_dispatch_next(self):
        """/done must auto-chain to /dispatch-next on success (self-feeding loop)."""
        _skip_if_missing(DONE)
        body = DONE.read_text()
        assert "/dispatch-next" in body, "/done does not chain to /dispatch-next"
        assert "Step 5" in body, "/done Step 5 (auto-chain) missing"


# ---------------------------------------------------------------------------
# /advisor-react — the advisor-side reaction poller
# ---------------------------------------------------------------------------


class TestAdvisorReact:
    def test_advisor_react_skill_exists(self):
        _skip_if_missing(ADVISOR_REACT)
        assert ADVISOR_REACT.exists()

    def test_advisor_react_polls_task_complete(self):
        """/advisor-react must poll session_messages for task_complete acks."""
        _skip_if_missing(ADVISOR_REACT)
        body = ADVISOR_REACT.read_text()
        assert "session_messages" in body
        assert "task_complete" in body
        assert "read=eq.false" in body, (
            "/advisor-react must filter to unread messages only"
        )

    def test_advisor_react_fires_advisor_plan_per_sender(self):
        """For each distinct completing worker, /advisor-plan must be fired."""
        _skip_if_missing(ADVISOR_REACT)
        body = ADVISOR_REACT.read_text()
        assert "/advisor-plan" in body or "advisor-plan" in body, (
            "/advisor-react does not trigger /advisor-plan"
        )

    def test_advisor_react_gated_to_advisor_role(self):
        """The reaction poller must no-op when run in a non-advisor terminal."""
        _skip_if_missing(ADVISOR_REACT)
        body = ADVISOR_REACT.read_text()
        assert "role=eq.advisor" in body or 'role="advisor"' in body or 'role=advisor' in body, (
            "/advisor-react does not gate itself to advisor-role sessions"
        )


# ---------------------------------------------------------------------------
# /advisor-plan — enriched task_handoff to the worker
# ---------------------------------------------------------------------------


class TestAdvisorPlan:
    def test_advisor_plan_skill_exists(self):
        _skip_if_missing(ADVISOR_PLAN)
        assert ADVISOR_PLAN.exists()

    def test_advisor_plan_emits_task_handoff(self):
        """/advisor-plan must wire a task_handoff back to the worker."""
        _skip_if_missing(ADVISOR_PLAN)
        body = ADVISOR_PLAN.read_text()
        assert "task_handoff" in body, (
            "/advisor-plan does not emit task_handoff — workers will miss enriched plans"
        )


# ---------------------------------------------------------------------------
# /dispatch-next — the worker-side handoff consumer
# ---------------------------------------------------------------------------


class TestDispatchNextConsumesHandoff:
    def test_dispatch_next_exists(self):
        _skip_if_missing(DISPATCH_NEXT)
        assert DISPATCH_NEXT.exists()

    def test_dispatch_next_polls_handoff_before_queue(self):
        """/dispatch-next Step 1 must check for task_handoff before falling through."""
        _skip_if_missing(DISPATCH_NEXT)
        body = DISPATCH_NEXT.read_text()
        # Step 1 should appear before Step 2 (lane-aware claim)
        assert "task_handoff" in body, (
            "/dispatch-next does not consume task_handoff messages"
        )
        step1_idx = body.find("Step 1")
        step2_idx = body.find("Step 2")
        assert step1_idx > 0 and step2_idx > step1_idx, (
            "/dispatch-next Step 1 must precede Step 2"
        )

    def test_dispatch_next_waits_for_advisor_enrichment(self):
        """Step 1b must wait briefly for an enriched handoff when advisor is alive."""
        _skip_if_missing(DISPATCH_NEXT)
        body = DISPATCH_NEXT.read_text()
        assert "Step 1b" in body, (
            "/dispatch-next Step 1b (enrichment wait) missing — loop will bypass advisor lane-continuity"
        )
        assert "advisor active" in body.lower() or "advisor_active" in body, (
            "/dispatch-next does not check advisor liveness before waiting"
        )


# ---------------------------------------------------------------------------
# lib/wire.sh — the shared transport
# ---------------------------------------------------------------------------


class TestWireLibSupportsAck:
    def test_wire_lib_exists(self):
        if not BATTLESTATION_WIRE.exists():
            pytest.skip("battlestation/lib/wire.sh missing")

    def test_wire_send_function_defined(self):
        if not BATTLESTATION_WIRE.exists():
            pytest.skip("battlestation/lib/wire.sh missing")
        body = BATTLESTATION_WIRE.read_text()
        assert re.search(r"^wire_send\s*\(\s*\)\s*\{", body, re.MULTILINE), (
            "wire_send function not defined in lib/wire.sh"
        )

    def test_wire_handoff_function_defined(self):
        if not BATTLESTATION_WIRE.exists():
            pytest.skip("battlestation/lib/wire.sh missing")
        body = BATTLESTATION_WIRE.read_text()
        assert re.search(r"^wire_handoff\s*\(\s*\)\s*\{", body, re.MULTILINE), (
            "wire_handoff function not defined — advisor can't send enriched plans"
        )
