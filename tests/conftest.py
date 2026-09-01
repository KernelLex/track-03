"""Suite-wide fixtures.

The one thing here is process-global state isolation. `agent.api.demo`
keeps several module-level dicts and counters (per-channel and per-number
cooldowns, the last-followed-up cursors, the cached payment URL) because
they're deliberately in-process for a demo surface rather than backed by a
store. Module-level state plus a module-scoped reset fixture is a latent
order-dependence: it holds only as long as the *only* module touching that
state is the one carrying the fixture.

An external audit reported three failures on a clean clone at a SHA that
ran green for me on the same clean clone -- I could not reproduce it at
any of four commits, with no project env vars set. Order dependence is the
explanation I couldn't rule out, and the honest response to "your global
state is protected by a fixture in one file" is to stop it being true
rather than to argue about whether it bit yet. Resetting here means every
test in the suite starts from the same state regardless of what ran
before it, or in what order.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_demo_module_state():
    """Reset agent.api.demo's process-global state around every test.

    Imported lazily inside the fixture: importing agent.api.demo at module
    scope would pull FastAPI, the Anthropic SDK, and the rails into every
    test session, including the many that have nothing to do with the API.
    """
    import agent.api.demo as demo

    def _clear() -> None:
        demo._last_triggered_at.clear()
        demo._last_triggered_at_by_number.clear()
        demo._last_followed_up_update_id = 0
        demo._last_followed_up_whatsapp_sid = None
        demo._last_payment_link_url = None
        demo._conversation_touches.clear()

    _clear()
    yield
    _clear()
