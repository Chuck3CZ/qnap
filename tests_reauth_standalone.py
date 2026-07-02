"""Standalone regression test for QnapCoordinator._get_containers_with_reauth().

This deliberately does NOT depend on the full Home Assistant test harness
(tests.common / pytest-homeassistant-custom-component fixtures), because
this repo's existing test suite is wired to run inside the actual
home-assistant/core checkout. Instead it constructs a QnapCoordinator
instance via object.__new__ (bypassing DataUpdateCoordinator.__init__,
which needs a live `hass` object) and manually attaches mocked `_api` /
`_cs` clients — exactly the two attributes the method under test touches.

This reproduces the real-world failure from the QTS logs:

    GET /api/v3/containers
    -> {"code":1002,"message":"unauthorized: you should add
        'Authorization: Bearer xxx' in header"}

which qnap-client surfaces as QnapAuthError, while every other endpoint
hit around the same time succeeds (because only the Container Station
call's session state was left stale by get_all()).

Run with:  python3 -m pytest tests_reauth_standalone.py -v
Requires:  pip install pytest pytest-asyncio qnap-client
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from qnap_client import QnapAuthError
from qnap_client.models import Container

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from custom_components.qnap.coordinator import QnapCoordinator  # noqa: E402


def _make_bare_coordinator() -> QnapCoordinator:
    """Build a QnapCoordinator without running DataUpdateCoordinator.__init__.

    We only need `_api` and `_cs` for the method under test, so we avoid
    needing a real HomeAssistant/ConfigEntry instance entirely.
    """
    coordinator = object.__new__(QnapCoordinator)
    coordinator._api = AsyncMock()
    coordinator._cs = AsyncMock()
    return coordinator


SAMPLE_CONTAINERS = [
    Container(id="c1", name="homeassistant", state="running",
              image="ghcr.io/home-assistant/home-assistant", type="docker"),
]


@pytest.mark.asyncio
async def test_no_reauth_needed_on_success() -> None:
    """Happy path: get_containers() succeeds first try, login() never called."""
    coordinator = _make_bare_coordinator()
    coordinator._cs.get_containers.return_value = SAMPLE_CONTAINERS

    result = await coordinator._get_containers_with_reauth()

    assert result == SAMPLE_CONTAINERS
    coordinator._api.login.assert_not_called()
    assert coordinator._cs.get_containers.call_count == 1


@pytest.mark.asyncio
async def test_reauths_once_and_retries_on_auth_error() -> None:
    """Reproduces the 1002 'add Authorization Bearer' failure from the logs.

    First call raises QnapAuthError (stale/missing session mid-update).
    The coordinator must call login() exactly once and retry, succeeding
    on the second attempt.
    """
    coordinator = _make_bare_coordinator()
    coordinator._cs.get_containers.side_effect = [
        QnapAuthError(
            "Container Station: Bearer token rejected "
            "(unauthorized: you should add 'Authorization: Bearer xxx' in header)"
        ),
        SAMPLE_CONTAINERS,
    ]

    result = await coordinator._get_containers_with_reauth()

    assert result == SAMPLE_CONTAINERS
    coordinator._api.login.assert_awaited_once()
    assert coordinator._cs.get_containers.call_count == 2


@pytest.mark.asyncio
async def test_gives_up_after_one_retry_still_unauthorized() -> None:
    """If re-login doesn't help (e.g. bad credentials), don't loop forever —
    the second QnapAuthError propagates to the caller (_async_update_data),
    which already treats Container Station failures as non-fatal.
    """
    coordinator = _make_bare_coordinator()
    coordinator._cs.get_containers.side_effect = [
        QnapAuthError("first failure"),
        QnapAuthError("still unauthorized after re-login"),
    ]

    with pytest.raises(QnapAuthError, match="still unauthorized"):
        await coordinator._get_containers_with_reauth()

    coordinator._api.login.assert_awaited_once()
    assert coordinator._cs.get_containers.call_count == 2


@pytest.mark.asyncio
async def test_non_auth_errors_are_not_retried() -> None:
    """A non-auth failure (e.g. connection error) should propagate immediately
    without triggering a pointless re-login attempt.
    """
    from qnap_client import QnapConnectionError

    coordinator = _make_bare_coordinator()
    coordinator._cs.get_containers.side_effect = QnapConnectionError("network down")

    with pytest.raises(QnapConnectionError):
        await coordinator._get_containers_with_reauth()

    coordinator._api.login.assert_not_called()
    assert coordinator._cs.get_containers.call_count == 1
