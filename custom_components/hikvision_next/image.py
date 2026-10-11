"""Image entities with camera snapshots."""

from datetime import datetime
import logging

import voluptuous as vol

from homeassistant.components.camera import Camera
from homeassistant.components.image import ENTITY_ID_FORMAT, ImageEntity
from homeassistant.const import ATTR_ENTITY_ID, CONF_FILENAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.template import Template
from homeassistant.util import slugify

from . import HikvisionConfigEntry
from .const import ACTION_UPDATE_SNAPSHOT, DOMAIN, FACE_VERIFY_IMAGE_SUFFIX
from .hikvision_device import HikvisionDevice
from .isapi import CameraStreamInfo

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: HikvisionConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add images with snapshots."""

    device = entry.runtime_data

    entities = []
    for camera in device.cameras:
        for stream in camera.streams:
            if stream.type_id == 1:
                entities.append(SnapshotFile(hass, device, camera, stream))

    entities.append(FaceVerifyImage(hass, device))
    async_add_entities(entities)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        ACTION_UPDATE_SNAPSHOT,
        {vol.Required(CONF_FILENAME): cv.template},
        "update_snapshot_filename",
    )


class SnapshotFile(ImageEntity):
    """An entity for displaying snapshot files."""

    _attr_has_entity_name = True
    file_path = None

    def __init__(
        self,
        hass: HomeAssistant,
        device: HikvisionDevice,
        camera: Camera,
        stream_info: CameraStreamInfo,
    ) -> None:
        """Initialize the snapshot file."""

        ImageEntity.__init__(self, hass)

        self._attr_unique_id = slugify(f"{device.device_info.serial_no.lower()}_{stream_info.id}_snapshot")
        self.entity_id = ENTITY_ID_FORMAT.format(self.unique_id)
        self._attr_translation_key = "snapshot"
        self._attr_translation_placeholders = {"camera": camera.name}

    def image(self) -> bytes | None:
        """Return bytes of image."""
        try:
            if self.file_path:
                with open(self.file_path, "rb") as file:
                    return file.read()
        except FileNotFoundError:
            _LOGGER.warning(
                "Could not read camera %s image from file: %s",
                self.name,
                self.file_path,
            )
        return None

    async def update_snapshot_filename(
        self,
        filename: Template,
    ) -> None:
        """Update the file_path."""
        self.file_path = filename.async_render(variables={ATTR_ENTITY_ID: self.entity_id})
        self._attr_image_last_updated = datetime.now()
        self.schedule_update_ha_state()


class FaceVerifyImage(ImageEntity):
    """Most recent inline JPEG supplied with an access-control face event."""

    _attr_has_entity_name = True
    _attr_name = "Face verification image"
    _attr_icon = "mdi:account-box"

    def __init__(self, hass: HomeAssistant, device: HikvisionDevice) -> None:
        """Initialize image entity."""
        ImageEntity.__init__(self, hass)
        self.device = device
        self._image: bytes | None = None
        self._attr_unique_id = slugify(
            f"{device.device_info.serial_no.lower()}_{FACE_VERIFY_IMAGE_SUFFIX}"
        )
        self._attr_device_info = device.hass_device_info()

    def image(self) -> bytes | None:
        """Return the latest received image bytes."""
        return self._image

    async def async_added_to_hass(self) -> None:
        """Register entity for SDK callbacks."""
        await super().async_added_to_hass()
        self.hass.data.setdefault(DOMAIN, {}).setdefault("face_verify_images", {})[
            self.unique_id
        ] = self

    async def async_will_remove_from_hass(self) -> None:
        """Unregister entity and discard the image."""
        images = self.hass.data.get(DOMAIN, {}).get("face_verify_images", {})
        images.pop(self.unique_id, None)
        self._image = None
        await super().async_will_remove_from_hass()

    def set_image(
        self,
        image: bytes,
        *,
        person_name: str | None = None,
        employee_no: str | None = None,
        card_no: str | None = None,
        pic_len: int | None = None,
    ) -> None:
        """Update the image and associated verification attributes."""
        self._image = image
        self._attr_image_last_updated = datetime.now()
        self._attr_extra_state_attributes = {
            key: value
            for key, value in {
                "name": person_name,
                "employee_no": employee_no,
                "card_no": card_no,
                "pic_data_len": pic_len,
            }.items()
            if value is not None
        }
        self.schedule_update_ha_state()
