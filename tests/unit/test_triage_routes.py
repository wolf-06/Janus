"""The triage decision rule: argmax, tau, epsilon, deterministic ties, and the vocabulary."""

from __future__ import annotations

import math

from janus.core.triage.routes import (
    COMMENT_ONLY_ROUTES,
    DEFAULT_EPSILON,
    DEFAULT_TAU,
    DEFAULT_WEIGHTS,
    SIGNAL_NAMES,
    TRIAGE_ROUTES,
    Route,
    decide,
)


def test_route_members() -> None:
    assert set(Route) == {Route.CODE_BUG, Route.FLAKY, Route.ENV, Route.UNSURE}


def test_comment_only_routes_exclude_code_bug() -> None:
    assert COMMENT_ONLY_ROUTES == frozenset({Route.FLAKY, Route.ENV, Route.UNSURE})
    assert Route.CODE_BUG not in COMMENT_ONLY_ROUTES


def test_defaults() -> None:
    assert DEFAULT_TAU == 0.80
    assert DEFAULT_EPSILON == 0.05


def test_triage_routes_are_the_three_real_routes_in_order() -> None:
    assert TRIAGE_ROUTES == (Route.CODE_BUG, Route.FLAKY, Route.ENV)
    assert Route.UNSURE not in TRIAGE_ROUTES


def test_signal_names_are_the_seven_and_match_the_weights() -> None:
    assert SIGNAL_NAMES == (
        "determinism",
        "signatures",
        "exit_codes",
        "resources",
        "change_attribution",
        "correlation",
        "flake_history",
    )
    assert set(DEFAULT_WEIGHTS) == set(SIGNAL_NAMES)
    assert math.isclose(sum(DEFAULT_WEIGHTS.values()), 1.0, rel_tol=1e-9, abs_tol=1e-9)


def test_argmax_above_tau_and_separated() -> None:
    decision = decide(
        {Route.CODE_BUG: 0.9, Route.FLAKY: 0.3, Route.ENV: 0.2},
        reasons=["determinism: always fails"],
    )

    assert decision.route is Route.CODE_BUG
    assert decision.confidence == 0.9
    assert math.isclose(decision.top2_gap, 0.6)
    assert decision.reasons[0] == "determinism: always fails"
    assert any("argmax" in reason for reason in decision.reasons)


def test_below_tau_is_unsure() -> None:
    decision = decide({Route.CODE_BUG: 0.7, Route.FLAKY: 0.2, Route.ENV: 0.1})

    assert decision.route is Route.UNSURE
    assert decision.confidence == 0.7
    assert math.isclose(decision.top2_gap, 0.5)
    assert any("below tau" in reason for reason in decision.reasons)


def test_within_epsilon_is_unsure_even_above_tau() -> None:
    decision = decide({Route.CODE_BUG: 0.9, Route.FLAKY: 0.87, Route.ENV: 0.1})

    assert decision.route is Route.UNSURE
    assert decision.confidence == 0.9
    assert math.isclose(decision.top2_gap, 0.03, abs_tol=1e-9)
    assert any("within epsilon" in reason for reason in decision.reasons)


def test_gap_exactly_epsilon_is_not_unsure() -> None:
    decision = decide({Route.CODE_BUG: 0.9, Route.FLAKY: 0.85, Route.ENV: 0.1})

    assert decision.route is Route.CODE_BUG


def test_exact_tie_resolves_to_the_earlier_route_in_order() -> None:
    decision = decide(
        {Route.CODE_BUG: 0.5, Route.FLAKY: 0.5, Route.ENV: 0.1},
        tau=0.0,
        epsilon=0.0,
    )

    assert decision.route is Route.CODE_BUG


def test_tie_between_flaky_and_env_prefers_flaky() -> None:
    decision = decide(
        {Route.FLAKY: 0.5, Route.ENV: 0.5},
        tau=0.0,
        epsilon=0.0,
    )

    assert decision.route is Route.FLAKY


def test_tie_is_unsure_under_default_epsilon() -> None:
    decision = decide({Route.CODE_BUG: 0.9, Route.FLAKY: 0.9})

    assert decision.route is Route.UNSURE
    assert decision.top2_gap == 0.0


def test_missing_routes_default_to_zero() -> None:
    decision = decide({Route.ENV: 0.9})

    assert decision.route is Route.ENV
    assert decision.confidence == 0.9
    assert decision.top2_gap == 0.9


def test_scores_mapping_carries_every_route() -> None:
    decision = decide({Route.CODE_BUG: 0.9, Route.FLAKY: 0.3, Route.ENV: 0.2})

    assert decision.scores == {
        Route.CODE_BUG: 0.9,
        Route.FLAKY: 0.3,
        Route.ENV: 0.2,
        Route.UNSURE: 0.0,
    }


def test_custom_tau_and_epsilon_are_honoured() -> None:
    decision = decide(
        {Route.CODE_BUG: 0.6, Route.FLAKY: 0.2, Route.ENV: 0.1},
        tau=0.5,
        epsilon=0.01,
    )

    assert decision.route is Route.CODE_BUG


def test_no_evidence_is_unsure() -> None:
    decision = decide({})

    assert decision.route is Route.UNSURE
    assert decision.confidence == 0.0
    assert decision.top2_gap == 0.0
