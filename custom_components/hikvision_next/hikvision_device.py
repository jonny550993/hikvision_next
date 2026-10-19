"ISAPI client for Home Assistant integration."

import logging
from typing import Any

import httpx
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.util import slugify

from .const import (
    ALARM_SERVER_PATH,
    BEHAVIOR_RULES_COORDINATOR,
    CONF_ALARM_SERVER_HOST,
    CONF_CONNECTION_SDK,
    CONF_SET_ALARM_SERVER,
    DEVICE_LEVEL_EVENT_IDS,
    DOMAIN,
    EVENTS,
    EVENTS_COORDINATOR,
    RTSP_PORT_FORCED,
    SECONDARY_COORDINATOR,
    resolve_connection_type,
)
from .coordinator import BehaviorRulesCoordinator, EventsCoordinator, SecondaryCoordinator
from .door_control import DoorControlAction
from .isapi import (
    BehaviorRuleInfo,
    EventInfo,
    IPCamera,
    ISAPIClient,
    ISAPIForbiddenError,
    ISAPIUnauthorizedError,
)
from .isapi.const import EVENT_IO
from .sdk.acs_door import door_index_from_event_url, sdk_remote_control_door

_LOGGER = logging.getLogger(__name__)

# HA 2026.9 deprecated DeviceInfo `via_device` and rejects it for entities added
# outside the integration's call stack; `via_device_id` replaces it.
SUPPORTS_VIA_DEVICE_ID = "via_device_id" in DeviceInfo.__annotations__


class HikvisionDevice(ISAPIClient):
    """Hikvision device for Home Assistant integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Initialize device."""

        config = entry.data if entry else data
        self.entry = entry
        self.hass = hass
        self.connection_type = resolve_connection_type(config)
        self.intercom_coordinator = None
        self.nvr_device_id: str | None = None  # device registry id of the NVR, set on setup
        self.auth_token_expired = False
        self.control_alarm_server_host = config[CONF_SET_ALARM_SERVER]
        self.alarm_server_host = config[CONF_ALARM_SERVER_HOST]

        # init ISAPI client
        host = config[CONF_HOST]
        username = config[CONF_USERNAME]
        password = config[CONF_PASSWORD]
        verify_ssl = config.get(CONF_VERIFY_SSL, True)
        rtsp_port_forced = config.get(RTSP_PORT_FORCED, None)
        session = get_async_client(hass, verify_ssl)
        super().__init__(host, username, password, verify_ssl, rtsp_port_forced, session)

        self.events_info: list[EventInfo] = []
        self.behavior_rules: list[BehaviorRuleInfo] = []

    async def init_coordinators(self):
        """Initialize coordinators."""

        # init events supported by integration
        self.events_info = self.get_device_event_capabilities()
        for camera in self.cameras:
            camera.events_info = self.get_device_event_capabilities(camera.id)

        # create coordinators
        self.coordinators = {}
        self.coordinators[EVENTS_COORDINATOR] = EventsCoordinator(self.hass, self)
        if (
            self.capabilities.support_holiday_mode
            or self.capabilities.support_alarm_server
            or self.capabilities.storage
        ):
            self.coordinators[SECONDARY_COORDINATOR] = SecondaryCoordinator(self.hass, self)

        if self.behavior_rules:
            self.coordinators[BEHAVIOR_RULES_COORDINATOR] = BehaviorRulesCoordinator(self.hass, self)

        if self.control_alarm_server_host and self.capabilities.support_alarm_server:
            await self.set_alarm_server(self.alarm_server_host, ALARM_SERVER_PATH)

        # first data fetch
        for coordinator in self.coordinators.values():
            await coordinator.async_config_entry_first_refresh()

    def hass_camera_device_info(self, camera: IPCamera) -> DeviceInfo:
        """Return Home Assistant device information for a camera entity."""
        return self.hass_device_info(camera.id)

    def hass_device_info(self, camera_id: int = 0) -> DeviceInfo:
        """Return Home Assistant entity device information."""
        if camera_id == 0:
            return DeviceInfo(
                manufacturer=self.device_info.manufacturer,
                identifiers={(DOMAIN, self.device_info.serial_no)},
                connections={(dr.CONNECTION_NETWORK_MAC, self.device_info.mac_address)},
                model=self.device_info.model,
                name=self.device_info.name,
                sw_version=self.device_info.firmware,
            )
        else:
            camera_info = self.get_camera_by_id(camera_id)
            is_ip_camera = isinstance(camera_info, IPCamera)

            return DeviceInfo(
                manufacturer=self.device_info.manufacturer,
                identifiers={(DOMAIN, camera_info.serial_no)},
                model=camera_info.model,
                name=camera_info.name,
                sw_version=camera_info.firmware if is_ip_camera else "Unknown",
                **self._nvr_link(),
            )

    def _nvr_link(self) -> DeviceInfo:
        """Link a camera device to its NVR device."""

        if not self.device_info.is_nvr:
            return DeviceInfo()
        if SUPPORTS_VIA_DEVICE_ID and self.nvr_device_id:
            return DeviceInfo(via_device_id=self.nvr_device_id)
        return DeviceInfo(via_device=(DOMAIN, self.device_info.serial_no))

    def get_device_event_capabilities(
        self,
        camera_id: int | None = None,
    ) -> list[EventInfo]:
        """Return integration-supported events scoped to the recorder or camera."""
        events: list[EventInfo] = []

        if camera_id is None:
            supported = [
                event
                for event in self.supported_events
                if event.id in EVENTS
                and (
                    EVENTS[event.id].get("type") == EVENT_IO
                    or event.id in DEVICE_LEVEL_EVENT_IDS
                )
                and not (self.device_info.is_nvr and event.id == "anpr" and event.channel_id == 0)
            ]
        else:
            supported = [
                event
                for event in self.supported_events
                if event.id in EVENTS and event.channel_id == int(camera_id)
            ]

        for event in supported:
            device_id_param = f"_{camera_id}" if camera_id else ""
            io_port_id_param = f"_{event.io_port_id}" if event.io_port_id != 0 else ""
            serial = slugify(self.device_info.serial_no.lower())
            event.unique_id = f"{serial}{device_id_param}{io_port_id_param}_{event.id}"
            event.disabled = "center" not in event.notifications
            if event.id == "anpr":
                event.anpr_plate_unique_id = (
                    f"{serial}{device_id_param}{io_port_id_param}_anpr_plate"
                )
                event.anpr_image_unique_id = (
                    f"{serial}{device_id_param}{io_port_id_param}_anpr_snap"
                )
            events.append(event)
        return events

    async def remote_control_door(self, event: EventInfo, action: DoorControlAction) -> None:
        """Control a door using the configured SDK connection when available, else ISAPI."""
        sdk_user = getattr(self, "sdk_user", None)
        sdk_subscription = getattr(self, "sdk_subscription", None)
        if self.connection_type == CONF_CONNECTION_SDK and sdk_user is not None and sdk_subscription:
            sdk = getattr(sdk_subscription, "sdk", sdk_subscription)
            await self.hass.async_add_executor_job(
                sdk_remote_control_door,
                sdk,
                sdk_user,
                door_index_from_event_url(event.url),
                action,
            )
            return
        await super().remote_control_door(event, action)

    def handle_exception(self, ex: Exception, details: str = ""):
        """Handle common exceptions."""

        error = "Unexpected exception"

        if isinstance(ex, ISAPIUnauthorizedError):
            if not self.auth_token_expired:
                # after device reboot, authorization token may have expired
                self.auth_token_expired = True
                self._auth_method = None
                self._session = get_async_client(self.hass, self.verify_ssl)
                _LOGGER.warning("Unauthorized access to %s, started checking if token expired", self.host)
                return
            self.auth_token_expired = False
            self.entry.async_start_reauth(self.hass)
            error = "Unauthorized access"
        elif isinstance(ex, ISAPIForbiddenError):
            error = "Forbidden access"
        elif isinstance(ex, (httpx.TimeoutException, httpx.ConnectTimeout)):
            error = "Timeout"
        elif isinstance(ex, (httpx.ConnectError, httpx.NetworkError)):
            error = "Connection error"

        _LOGGER.warning("%s | %s | %s | %s", error, self.host, details, ex)
