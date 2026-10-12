"""The scorer: weighted aggregation over present signals, and the decision delegation.

Covers the two invariants the aggregation must hold — normalise by the weights of the signals
that actually reported (an abstention must not dilute), and never let a single signal push a
route above ``1.0`` — plus the ``score`` → ``decide`` path (clear route, below-``tau``,
within-``epsilon``, and the no-evidence case).
"""

from __future__ import annotations

import math

from janus.core.signals.base import SignalEvidence
from janus.core.triage.routes import DEFAULT_WEIGHTS, TRIAGE_ROUTES, Route
from janus.core.triage.scorer import aggregate, score


def evidence(name: str, votes: dict[Route, float], summary: str = "observed") -> SignalEvidence:
    return SignalEvidence(name=name, summary=summary, details={}, votes=votes)


def test_aggregate_is_the_weighted_average_over_present_signals() -> None:
    determinism = evidence("determinism", {Route.CODE_BUG: 1.0})
    signatures = evidence("signatures", {Route.CODE_BUG: 0.5})

    scores = aggregate([determinism, signatures])

    expected = (DEFAULT_WEIGHTS["determinism"] * 1.0 + DEFAULT_WEIGHTS["signatures"] * 0.5) / (
        DEFAULT_WEIGHTS["determinism"] + DEFAULT_WEIGHTS["signatures"]
    )
    assert math.isclose(scores[Route.CODE_BUG], expected)
    assert math.isclose(scores[Route.CODE_BUG], 0.775)
    assert scores[Route.FLAKY] == 0.0
    assert scores[Route.ENV] == 0.0


def test_aggregate_normalises_by_present_weights_only() -> None:
    scores = aggregate([evidence("determinism", {Route.CODE_BUG: 1.0})])

    assert scores[Route.CODE_BUG] == 1.0


def test_aggregate_ignores_a_signal_whose_name_is_not_in_the_weights() -> None:
    scores = aggregate([evidence("not_a_signal", {Route.CODE_BUG: 1.0})])

    assert scores == {route: 0.0 for route in TRIAGE_ROUTES}


def test_aggregate_ignores_an_unknown_signal_alongside_a_known_one() -> None:
    scores = aggregate(
        [
            evidence("determinism", {Route.CODE_BUG: 1.0}),
            evidence("not_a_signal", {Route.FLAKY: 1.0}),
        ]
    )

    assert scores[Route.CODE_BUG] == 1.0
    assert scores[Route.FLAKY] == 0.0


def test_aggregate_of_empty_evidence_is_all_zeros() -> None:
    scores = aggregate([])

    assert scores == {route: 0.0 for route in TRIAGE_ROUTES}


def test_aggregate_never_produces_unsure() -> None:
    scores = aggregate([evidence("determinism", {Route.CODE_BUG: 1.0})])

    assert set(scores) == set(TRIAGE_ROUTES)
    assert Route.UNSURE not in scores


def test_aggregate_ignores_an_unsure_vote() -> None:
    scores = aggregate([evidence("determinism", {Route.UNSURE: 1.0})])

    assert scores == {route: 0.0 for route in TRIAGE_ROUTES}


def test_aggregate_single_strong_signal_does_not_exceed_one() -> None:
    scores = aggregate([evidence("determinism", {Route.CODE_BUG: 1.0})])

    assert scores[Route.CODE_BUG] <= 1.0


def test_aggregate_clamps_out_of_range_votes() -> None:
    scores = aggregate([evidence("determinism", {Route.CODE_BUG: 1.5, Route.FLAKY: -0.5})])

    assert scores[Route.CODE_BUG] == 1.0
    assert scores[Route.FLAKY] == 0.0


def test_score_clear_code_bug() -> None:
    decision = score([evidence("determinism", {Route.CODE_BUG: 1.0})])

    assert decision.route is Route.CODE_BUG
    assert decision.confidence == 1.0
    assert decision.top2_gap == 1.0


def test_score_below_tau_is_unsure() -> None:
    decision = score([evidence("determinism", {Route.CODE_BUG: 0.7})])

    assert decision.route is Route.UNSURE
    assert decision.confidence == 0.7
    assert any("below tau" in reason for reason in decision.reasons)


def test_score_within_epsilon_is_unsure() -> None:
    decision = score([evidence("determinism", {Route.CODE_BUG: 0.9, Route.FLAKY: 0.9})])

    assert decision.route is Route.UNSURE
    assert decision.confidence == 0.9
    assert decision.top2_gap == 0.0
    assert any("within epsilon" in reason for reason in decision.reasons)


def test_score_with_no_evidence_is_unsure_at_zero_confidence() -> None:
    decision = score([])

    assert decision.route is Route.UNSURE
    assert decision.confidence == 0.0
    assert any("no signal evidence" in reason for reason in decision.reasons)


def test_score_prefixes_each_reason_with_its_signal_name() -> None:
    decision = score([evidence("determinism", {Route.CODE_BUG: 1.0}, summary="3/3 reruns failed")])

    assert any(reason.startswith("determinism:") for reason in decision.reasons)
    assert any("3/3 reruns failed" in reason for reason in decision.reasons)


def test_score_bounds_each_reason() -> None:
    summary = "x" * 500

    decision = score([evidence("determinism", {Route.CODE_BUG: 1.0}, summary=summary)])

    reason = next(reason for reason in decision.reasons if reason.startswith("determinism:"))
    assert len(reason) < len(summary)
    assert reason.endswith("...")


def test_score_ignores_a_signal_with_an_unknown_name() -> None:
    decision = score([evidence("mystery", {Route.CODE_BUG: 1.0})])

    assert decision.route is Route.UNSURE
    assert decision.confidence == 0.0


def test_score_honours_custom_tau_and_epsilon() -> None:
    decision = score(
        [evidence("determinism", {Route.CODE_BUG: 0.6, Route.FLAKY: 0.2})],
        tau=0.5,
        epsilon=0.01,
    )

    assert decision.route is Route.CODE_BUG
