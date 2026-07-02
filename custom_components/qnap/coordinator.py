"""Data coordinator for the qnap integration."""

from __future__ import annotations

from datetime import timedelta
import logging

from qnap_client import ContainerStationClient, QnapAuthError, QnapClient, QnapError
from qnap_client.models import Container, NasData

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_TIMEOUT,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_PORT,
    DEFAULT_SSL,
    DEFAULT_TIMEOUT,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

type QnapConfigEntry = ConfigEntry[QnapCoordinator]

UPDATE_INTERVAL = timedelta(minutes=5)

_LOGGER = logging.getLogger(__name__)


class QnapCoordinator(DataUpdateCoordinator[NasData]):
    """Custom coordinator for the qnap integration."""

    config_entry: QnapConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: QnapConfigEntry) -> None:
        """Initialize the qnap coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )

        self._api = QnapClient(
            host=config_entry.data[CONF_HOST],
            port=config_entry.data.get(CONF_PORT, DEFAULT_PORT),
            username=config_entry.data[CONF_USERNAME],
            password=config_entry.data[CONF_PASSWORD],
            ssl=config_entry.data.get(CONF_SSL, DEFAULT_SSL),
            verify_ssl=config_entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            timeout=config_entry.data.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        )
        self._cs = ContainerStationClient(self._api)

    async def _async_setup(self) -> None:
        """Authenticate on first setup."""
        try:
            await self._api.login()
        except QnapError as err:
            raise ConfigEntryNotReady(f"Cannot connect to QNAP NAS: {err}") from err

    async def _async_update_data(self) -> NasData:
        """Fetch all NAS data."""
        try:
            data = await self._api.get_all()
        except QnapError as err:
            raise UpdateFailed(f"Error fetching QNAP data: {err}") from err

        # Container Station — non-fatal
        try:
            data.containers = await self._get_containers_with_reauth()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Container Station unavailable: %s", err)
            data.containers = getattr(self.data, "containers", []) if self.data else []

        return data

    async def _get_containers_with_reauth(self) -> list[Container]:
        """Fetch containers, retrying once after a fresh login on auth failure.

        Works around a known QTS behaviour: ``self._api.get_all()`` fires
        roughly a dozen CGI requests in parallel via ``asyncio.gather``. If
        the QTS session gets invalidated mid-batch — e.g. because the same
        account logs in concurrently through the web UI — the underlying
        qnap-client library quietly resets its session state for the one
        request that hit the 401, but ``get_all()`` as a whole still
        "succeeds" (the failure is swallowed per-field).

        The very next call, ``ContainerStationClient.get_containers()``,
        then either has no valid session to attach as a Bearer token or
        gets one rejected by Container Station's v3 API with::

            {"code": 1002, "message": "unauthorized: you should add
             'Authorization: Bearer xxx' in header"}

        ``ContainerStationClient`` raises :class:`QnapAuthError` in both
        cases, so a single fresh ``self._api.login()`` plus one retry is
        enough to recover without needing any change to the qnap-client
        library itself.
        """
        try:
            return await self._cs.get_containers()
        except QnapAuthError:
            _LOGGER.debug(
                "Container Station rejected the current QTS session; "
                "re-authenticating and retrying once"
            )
            await self._api.login()
            return await self._cs.get_containers()
