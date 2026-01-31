"""Support for EnOcean buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SIGNAL_ADD_ENTITIES
from .const import DATA_ENOCEAN, ENOCEAN_DONGLE
from .entity import (
    DynamicEnoceanEntity,
    EnOceanEntity,
    async_create_entities_from_eep,
    format_device_id_hex_underscore,
)
from .types import EEPEntityDef


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EnOcean button entities."""
    enocean_data = hass.data.get(DATA_ENOCEAN, {})
    dongle = enocean_data.get(ENOCEAN_DONGLE)

    if not dongle:
        return

    # Register listener to add button entities discovered via EEP
    async def _add_buttons_from_eep(
        device_id, entities_list, rorg, rorg_func, rorg_type
    ):
        def _kwargs_factory(
            ent: EEPEntityDef | None,
            device_id,
            device_name,
            rorg_int,
            func_int,
            type_int,
            description,
        ):
            # Extract channel/offset and a human name for the button
            if isinstance(ent, EEPEntityDef):
                channel = ent.offset
                name = ent.name or (
                    f"Button {channel}" if channel is not None else None
                )
            else:
                channel = getattr(ent, "offset", None)
                name = getattr(ent, "name", None) or (
                    f"Button {channel}" if channel is not None else None
                )

            if channel is None:
                return None

            try:
                return {"channel": int(channel), "button_name": name}
            except (TypeError, ValueError):
                return None

        await async_create_entities_from_eep(
            hass,
            config_entry,
            device_id,
            entities_list,
            rorg,
            rorg_func,
            rorg_type,
            platform_type="button",
            entity_class=DynamicEnOceanButton,
            async_add_entities=async_add_entities,
            entity_kwargs_factory=_kwargs_factory,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ADD_ENTITIES, _add_buttons_from_eep)
    )


class EnOceanButton(EnOceanEntity, ButtonEntity):
    """Representation of an EnOcean button device."""

    def __init__(
        self,
        dev_id: list[int],
        dev_id_hex: str,
        dev_name: str,
        channel: int,
        button_name: str,
    ) -> None:
        """Initialize the EnOcean button device."""
        super().__init__(
            dev_id, data_field=button_name, attr_name=button_name, dev_name=dev_name
        )
        self._dev_id_hex = dev_id_hex
        self.channel = channel
        self._attr_unique_id = f"{format_device_id_hex_underscore(dev_id)}-{channel}"
        self._attr_name = f"{dev_name} {button_name}"

    async def async_press(self) -> None:
        """Press the button."""
        optional = [0x03]
        optional.extend(self.dev_id)
        optional.extend([0xFF, 0x00])
        self.send_command(
            data=[0xF6, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
            optional=optional,
            packet_type=0x01,
        )


class DynamicEnOceanButton(DynamicEnoceanEntity, EnOceanButton):
    """Representation of a dynamic EnOcean button device."""

    def __init__(
        self,
        dev_id: list[int],
        dev_id_hex: str,
        dev_name: str,
        channel: int,
        button_name: str,
        rorg: int,
        rorg_func: int,
        rorg_type: int,
        fields: EEPEntityDef | None = None,
        command: int | None = None,
    ) -> None:
        """Initialize the dynamic EnOcean button device."""
        super().__init__(
            dev_id=dev_id,
            dev_name=dev_name,
            data_field=f"{button_name}-{channel}",
            rorg=rorg,
            rorg_func=rorg_func,
            rorg_type=rorg_type,
            attr_name=button_name,
            command=command,
            fields=fields,
        )
        # Override button-specific attributes set by EnOceanButton.__init__ indirectly
        self._dev_id_hex = dev_id_hex
        self.channel = channel
        self._attr_unique_id = f"{format_device_id_hex_underscore(dev_id)}-{channel}"
        self._attr_name = f"{dev_name} {button_name}"
