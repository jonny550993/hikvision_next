"""Platform for sensor integration."""

from __future__ import annotations

import asyncio

from homeassistant.components.sensor import ENTITY_ID_FORMAT, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from . import HikvisionConfigEntry
from .const import CONF_ALARM_SERVER_HOST, DOMAIN, FACE_PERSON_PULSE_SECONDS, SECONDARY_COORDINATOR
from .isapi import EventInfo, StorageInfo

NOTIFICATION_HOST_KEYS = [
    "protocol_type",
    "address", # ip_address or host_name
    "port_no",
    "path",
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HikvisionConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add diagnostic sensors for hikvision alarm server settings and storage items."""

    device = entry.runtime_data
    coordinator = device.coordinators.get(SECONDARY_COORDINATOR)

    entities = []
    for camera in device.cameras:
        for event in camera.events_info:
            if event.id == "face":
                entities.append(FacePersonSensor(device, camera.id, event))
    for event in device.events_info:
        if event.id == "face":
            entities.append(FacePersonSensor(device, 0, event))
    if coordinator:
        for key in NOTIFICATION_HOST_KEYS:
            entities.append(AlarmServerSensor(coordinator, key))

        for item in list(device.storage):
            entities.append(StorageSensor(coordinator, item))

        async_add_entities(entities, True)


class AlarmServerSensor(CoordinatorEntity, SensorEntity):
    """Alarm Server settings sensor."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:ip-network"

    def __init__(self, coordinator, key: str) -> None:
        """Initialize."""
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.device_info.serial_no}_{CONF_ALARM_SERVER_HOST}_{key}"
        self.entity_id = ENTITY_ID_FORMAT.format(slugify(self.unique_id))
        self._attr_device_info = device.hass_device_info()
        self._attr_translation_key = f"notifications_host_{key}"
        self.key = key

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor."""
        host = self.coordinator.data.get(CONF_ALARM_SERVER_HOST)
        return host.get(self.key)


class StorageSensor(CoordinatorEntity, SensorEntity):
    """HDD, NAS status sensor."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:harddisk"

    def __init__(self, coordinator, hdd: StorageInfo) -> None:
        """Initialize."""
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_unique_id = f"{device.device_info.serial_no}_{hdd.id}_{hdd.name}"
        self.entity_id = ENTITY_ID_FORMAT.format(slugify(self.unique_id))
        self._attr_device_info = device.hass_device_info()
        self._attr_name = f"{hdd.type} {hdd.name}"
        self.hdd = hdd

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor."""
        hdd = self.coordinator.device.get_storage_device_by_id(self.hdd.id)
        return str(hdd.status).upper() if hdd else None

    @property
    def extra_state_attributes(self):
        """Return extra attributes."""
        attrs = {}
        attrs["type"] = self.hdd.type
        attrs["capacity"] = self.hdd.capacity
        attrs["freespace"] = self.hdd.freespace
        if self.hdd.ip:
            attrs["ip"] = self.hdd.ip
        return attrs



class FacePersonSensor(SensorEntity):
    """Text sensor showing the most recently recognized face."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:account-check"
    _attr_native_value = "unknown"

    def __init__(self, device, camera_id: int, event: EventInfo) -> None:
        """Initialize the face recognition sensor."""
        self.device = device
        self.camera_id = camera_id
        self.event = event
        self._attr_unique_id = event.unique_id
        self._attr_device_info = device.hass_device_info(camera_id)
        self._attr_name = "Recognized person"
        self._attr_extra_state_attributes = {}
        self._face_reset_task: asyncio.Task | None = None

    async def async_added_to_hass(self) -> None:
        """Register this entity for event notifications."""
        await super().async_added_to_hass()
        self.hass.data.setdefault(DOMAIN, {}).setdefault("event_text_sensors", {})[
            self.unique_id
        ] = self

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending reset and unregister the entity."""
        self._cancel_face_reset()
        sensors = self.hass.data.get(DOMAIN, {}).get("event_text_sensors", {})
        sensors.pop(self.unique_id, None)
        await super().async_will_remove_from_hass()

    def set_person(self, person_name: str, attributes: dict) -> None:
        """Display the recognized person briefly."""
        self._cancel_face_reset()
        self._attr_native_value = person_name or "unknown"
        self._attr_extra_state_attributes = dict(attributes)
        self.async_write_ha_state()
        if self.hass:
            self._face_reset_task = self.hass.async_create_task(self._reset_after_pulse())

    async def _reset_after_pulse(self) -> None:
        """Clear personal data after the short recognition pulse."""
        try:
            await asyncio.sleep(FACE_PERSON_PULSE_SECONDS)
            self._attr_native_value = "unknown"
            self._attr_extra_state_attributes = {}
            self.async_write_ha_state()
        except asyncio.CancelledError:
            raise
        finally:
            self._face_reset_task = None

    def _cancel_face_reset(self) -> None:
        """Cancel a pending face reset task."""
        if self._face_reset_task and not self._face_reset_task.done():
            self._face_reset_task.cancel()
        self._face_reset_task = None
