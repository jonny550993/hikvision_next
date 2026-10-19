"""Events listener."""

from __future__ import annotations

from http import HTTPStatus
import ipaddress
import logging
import socket
from urllib.parse import urlparse

from aiohttp import web
from requests_toolbelt.multipart import MultipartDecoder

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import CONTENT_TYPE_TEXT_PLAIN, STATE_OFF, STATE_ON, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_registry import async_get
from homeassistant.util import slugify

from .const import ALARM_SERVER_PATH, DOMAIN, HIKVISION_EVENT
from .hikvision_device import HikvisionDevice
from .isapi import AlertInfo, IPCamera, ISAPIClient
from .isapi.const import EVENT_IO

_LOGGER = logging.getLogger(__name__)

CONTENT_TYPE = "Content-Type"
CONTENT_TYPE_TEXT_HTML = "text/html"
CONTENT_TYPE_IMAGE = "image/jpeg"


class EventNotificationsView(HomeAssistantView):
    """Event notifications listener."""

    def __init__(self, hass: HomeAssistant):
        """Initialize."""
        self.requires_auth = False
        self.url = ALARM_SERVER_PATH
        self.name = DOMAIN
        self.device: HikvisionDevice
        self.hass = hass

    async def post(self, request: web.Request):
        """Accept the POST request from NVR or IP Camera."""

        try:
            _LOGGER.debug("--- Incoming event notification ---")
            _LOGGER.debug("Source: %s", request.remote)
            xml = await self.parse_event_request(request)
            _LOGGER.debug("alert info: %s", xml)
            alert = ISAPIClient.parse_event_notification(xml)
            self.device = self.get_isapi_device(request.remote, alert)
            self.update_alert_channel(alert)
            self.trigger_sensor(alert)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("Cannot process incoming event %s", ex)

        response = web.Response(status=HTTPStatus.OK, content_type=CONTENT_TYPE_TEXT_PLAIN)
        return response

    def get_isapi_device(self, device_ip, alert: AlertInfo) -> HikvisionDevice:
        """Get integration instance for device sending alert."""
        integration_entries = self.hass.config_entries.async_entries(DOMAIN)
        instance_identifiers = []
        entry = None
        if len(integration_entries) == 1:
            entry = integration_entries[0]
        else:
            # Search device by mac_address
            for item in integration_entries:
                if item.disabled_by:
                    continue

                item_mac_address = item.runtime_data.device_info.mac_address
                instance_identifiers.append(item_mac_address)

                if item_mac_address == alert.mac:
                    entry = item
                    break

            # Search device by ip_address
            if not entry:
                for item in integration_entries:
                    if item.disabled_by:
                        continue

                    url = item.runtime_data.host
                    instance_identifiers.append(url)

                    if self.get_ip(urlparse(url).hostname) == device_ip:
                        entry = item
                        break

        if not entry:
            raise ValueError(f"Cannot find ISAPI instance for device {device_ip} in {instance_identifiers}")

        return entry.runtime_data

    def get_ip(self, ip_string: str) -> str:
        """Return an IP if either hostname or IP is provided."""

        try:
            ipaddress.ip_address(ip_string)
            return ip_string
        except ValueError:
            resolved_hostname = socket.gethostbyname(ip_string)
            _LOGGER.debug("Resolve host %s resolves to IP %s", ip_string, resolved_hostname)

            return resolved_hostname

    async def parse_event_request(self, request: web.Request) -> str:
        """Extract XML content from multipart request or from simple request."""

        data = await request.read()

        content_type_header = request.headers.get(CONTENT_TYPE, "").strip()
        if not content_type_header:
            raise ValueError("Missing event Content-Type header")

        _LOGGER.debug("request headers: %s", request.headers)
        xml = None
        media_type = content_type_header.split(";", 1)[0].strip().lower()
        xml_media_types = {"application/xml", "text/xml"}
        if media_type in xml_media_types:
            xml = data.decode("utf-8")
        elif media_type == "multipart/form-data":
            # "multipart/form-data; boundary=boundary"
            decoder = MultipartDecoder(data, content_type_header)
            for part in decoder.parts:
                headers = {}
                for key, value in part.headers.items():
                    assert isinstance(key, bytes)
                    headers[key.decode("ascii").lower()] = value.decode("ascii")
                _LOGGER.debug("part headers: %s", headers)
                part_content_type = headers.get(CONTENT_TYPE.lower(), "").split(";", 1)[0].strip().lower()
                if part_content_type in xml_media_types:
                    xml = part.text
                if part_content_type == CONTENT_TYPE_IMAGE:
                    _LOGGER.debug("image found")
                    # Use camera.snapshot service instead
                    # from datetime import datetime
                    # import aiofiles
                    # now = datetime.now()
                    # filename = f"/media/{DOMAIN}/snapshots/{now.strftime('%Y-%m-%d_%H-%M-%S_%f')}.jpg"
                    # async with aiofiles.open(filename, "wb") as image_file:
                    #     await image_file.write(part.content)
                    #     await image_file.flush()

        elif media_type not in xml_media_types:
            raise ValueError(f"Unsupported event Content-Type {content_type_header}")
        
        if not xml:
            raise ValueError(f"Unexpected event Content-Type {content_type_header}")
        return xml

    def update_alert_channel(self, alert: AlertInfo) -> AlertInfo:
        """Fix channel id for NVR/DVR alert."""

        if alert.channel_id > 32:
            # channel id above 32 is an IP camera
            # On DVRs that support analog cameras 33 may not be
            # camera 1 but camera 5 for example
            try:
                alert.channel_id = [
                    camera.id
                    for camera in self.device.cameras
                    if isinstance(camera, IPCamera) and camera.input_port == alert.channel_id - 32
                ][0]
            except IndexError:
                alert.channel_id = alert.channel_id - 32

    def _event_platform_unique_id(self, alert: AlertInfo) -> str:
        """Build the entity ID used by non-binary event platforms."""
        serial_no = slugify(self.device.device_info.serial_no.lower())
        device_id_param = f"_{alert.channel_id}" if alert.channel_id else ""
        io_port_id_param = f"_{alert.io_port_id}" if alert.io_port_id else ""
        return f"{alert.event_id}.{serial_no}{device_id_param}{io_port_id_param}_{alert.event_id}"

    def _lookup_event_entity(self, alert: AlertInfo):
        """Find a text entity for an event, if this event has one."""
        if alert.event_id not in {"face"}:
            return None, None
        sensors = self.hass.data.get(DOMAIN, {}).get("event_text_sensors", {})
        candidates = [
            (uid, entity)
            for uid, entity in sensors.items()
            if getattr(entity, "camera_id", 0) == alert.channel_id
        ]
        if not candidates and (alert.channel_id == 0 or len(sensors) == 1):
            candidates = list(sensors.items())
        if not candidates:
            return None, None
        uid, entity = candidates[0]
        return entity, uid

    def trigger_sensor(self, alert: AlertInfo) -> None:
        """Update text sensors or determine entity and set binary sensor state."""

        _LOGGER.debug("Alert: %s", alert)

        if alert.event_id == "lock":
            unique_id = self._event_platform_unique_id(alert).removeprefix("lock.")
            groups = self.hass.data.get(DOMAIN, {}).get("event_lock_groups", {})
            locked = alert.state != STATE_ON
            for entity in groups.get(unique_id, []):
                entity.set_locked(locked)
            return

        if alert.event_id == "face":
            entity, _lookup_uid = self._lookup_event_entity(alert)
            if entity is not None:
                person_name = alert.face_person_name or alert.state or alert.face_employee_no or "unknown"
                attributes = {
                    key: value
                    for key, value in {
                        "name": alert.face_person_name,
                        "employee_no": alert.face_employee_no,
                        "card_no": alert.face_card_no,
                    }.items()
                    if value is not None
                }
                entity.set_person(person_name, attributes)
                return

        serial_no = self.device.device_info.serial_no.lower()

        device_id_param = f"_{alert.channel_id}" if alert.channel_id != 0 and alert.event_id != EVENT_IO else ""
        io_port_id_param = f"_{alert.io_port_id}" if alert.io_port_id != 0 else ""
        unique_id = f"binary_sensor.{slugify(serial_no)}{device_id_param}{io_port_id_param}_{alert.event_id}"

        _LOGGER.debug("UNIQUE_ID: %s", unique_id)

        entity_registry = async_get(self.hass)
        entity_id = entity_registry.async_get_entity_id(Platform.BINARY_SENSOR, DOMAIN, unique_id)
        if entity_id:
            entity = self.hass.states.get(entity_id)
            if entity:
                self.hass.states.async_set(entity_id, STATE_ON, entity.attributes)
                self.fire_hass_event(alert)
            return
        raise ValueError(f"Entity not found {entity_id}")

    async def update_face_verify_image(
        self,
        device: HikvisionDevice,
        image: bytes,
        *,
        person_name: str | None = None,
        employee_no: str | None = None,
        card_no: str | None = None,
        pic_len: int | None = None,
    ) -> None:
        """Send an inline SDK face snapshot to the image entity."""
        images = self.hass.data.get(DOMAIN, {}).get("face_verify_images", {})
        if not images:
            return
        serial = slugify(f"{device.device_info.serial_no.lower()}_face_verify")
        entity = images.get(serial)
        if entity is None:
            entity = next(
                (
                    item for item in images.values()
                    if getattr(getattr(item, "device", None), "device_info", None)
                    and item.device.device_info.serial_no == device.device_info.serial_no
                ),
                None,
            )
        if entity is None:
            _LOGGER.debug("No face verification image entity registered for %s", device.device_info.serial_no)
            return
        entity.set_image(
            image,
            person_name=person_name,
            employee_no=employee_no,
            card_no=card_no,
            pic_len=pic_len,
        )

    def fire_hass_event(self, alert: AlertInfo):
        """Fire HASS event."""
        camera_name = ""
        if camera := self.device.get_camera_by_id(alert.channel_id):
            camera_name = camera.name

        message = {
            "channel_id": alert.channel_id,
            "io_port_id": alert.io_port_id,
            "camera_name": camera_name,
            "event_id": alert.event_id,
        }
        if alert.detection_target:
            message["detection_target"] = alert.detection_target
            message["region_id"] = alert.region_id

        self.hass.bus.fire(
            HIKVISION_EVENT,
            message,
        )
