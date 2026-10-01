"""The Chinese label tables, and the one property that makes them useful.

These helpers sit between a wire vocabulary that must stay English and prose
that must not be. The failure they guard against is not "wrong translation" but
"no translation": a miss returns the raw token, and the token then lands inside
an otherwise Chinese sentence — `WAITING_APPROVAL 已升级` reads like a bug,
because it is one.

The casing case is why these tests exist at all. `zh_incident_status` is reached
both with the enum and with a title-cased string, and a plain dict lookup only
answers one of those.
"""

from __future__ import annotations

import pytest

from opspilot_backend.domain.enums import (
    DiagnosisOutcome,
    IncidentStatus,
    RiskLevel,
    zh_incident_status,
    zh_outcome,
    zh_risk,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (IncidentStatus.ESCALATED, "已升级"),
        (IncidentStatus.ESCALATED.value, "已升级"),
        # The form that a plain lookup misses.
        ("Escalated", "已升级"),
        ("escalated", "已升级"),
        (IncidentStatus.WAITING_APPROVAL, "等待审批"),
        ("Waiting_approval", "等待审批"),
    ],
)
def test_zh_incident_status_ignores_casing(value: object, expected: str) -> None:
    assert zh_incident_status(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (RiskLevel.CRITICAL, "极高"),
        (RiskLevel.CRITICAL.value, "极高"),
        ("Critical", "极高"),
    ],
)
def test_zh_risk_ignores_casing(value: object, expected: str) -> None:
    assert zh_risk(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (DiagnosisOutcome.ROOT_CAUSE_PROBABLE, "根因很可能成立"),
        ("Root_cause_probable", "根因很可能成立"),
        # A run that never reached a verdict denormalises its outcome to
        # `UNKNOWN`, which is not a `DiagnosisOutcome` member but is still
        # rendered. It has to read as a sentence.
        ("UNKNOWN", "未得出结论"),
        ("Unknown", "未得出结论"),
    ],
)
def test_zh_outcome_covers_the_denormalised_verdict(value: object, expected: str) -> None:
    assert zh_outcome(value) == expected


def test_untranslatable_values_pass_through_rather_than_vanishing() -> None:
    """A value nobody translated must still be visible.

    Returning the token is the point: an operator seeing `SOMETHING_NEW` in the
    timeline can look it up. Substituting a placeholder such as "unknown" would
    hide a vocabulary that grew without its label table growing with it.
    """
    assert zh_incident_status("SOMETHING_NEW") == "SOMETHING_NEW"
    assert zh_risk("SOMETHING_NEW") == "SOMETHING_NEW"
    assert zh_outcome("SOMETHING_NEW") == "SOMETHING_NEW"


def test_every_wire_member_has_a_label() -> None:
    """Guards the tables against gaining a member without gaining a label."""
    for member in IncidentStatus:
        assert zh_incident_status(member) != member.value
    for member in RiskLevel:
        assert zh_risk(member) != member.value
    for member in DiagnosisOutcome:
        assert zh_outcome(member) != member.value
