"""§5.4's conformance suite, run against SimulatedRail today. The exact same
`run_conformance_suite` function is what a future test would call with a
`RazorpayRail` factory once test keys exist -- nothing here is
SimulatedRail-specific except the factory passed in.
"""

from __future__ import annotations

from agent.rails.conformance.suite import run_conformance_suite
from agent.rails.simulated import SimulatedRail


def test_simulated_rail_passes_its_own_conformance_suite():
    report = run_conformance_suite(lambda secret: SimulatedRail(webhook_secret=secret))
    assert report.rail_tag == "simulated"
    assert report.all_passed, [(c.name, c.detail) for c in report.failures]


def test_every_in_scope_check_actually_ran_something_meaningful():
    """Guards against a suite that trivially "passes" by skipping everything --
    at least the shape and webhook checks must be genuinely in-scope, not
    quietly marked skip."""
    report = run_conformance_suite(lambda secret: SimulatedRail(webhook_secret=secret))
    in_scope_names = {c.name for c in report.checks if c.in_scope}
    assert {"order_shape", "payment_link_shape", "invoice_shape",
            "mandate_shape_and_revoke_transition", "webhook_structure_and_signature",
            "idempotent_redelivery"}.issubset(in_scope_names)


def test_report_names_every_failure_with_a_detail_string():
    report = run_conformance_suite(lambda secret: SimulatedRail(webhook_secret=secret))
    for failure in report.failures:
        assert failure.detail
