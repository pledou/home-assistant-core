"""Support for EnOcean buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DATA_ENOCEAN, ENOCEAN_DONGLE
from .entity import DynamicEnoceanEntity, EnOceanEntity, async_create_entities_from_eep
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

    # Register callback to add button entities discovered via EEP
    async def _add_buttons_from_eep(
        device_id, entities_list, rorg, rorg_func, rorg_type
    ):
        def _kwargs_factory(ent: EEPEntityDef | None):
            # Extract channel/offset and a human name for the button
            if isinstance(ent, EEPEntityDef):
                channel = ent.offset
                description = ent.description or (
                    f"Button {channel}" if channel is not None else None
                )
            else:
                channel = getattr(ent, "offset", None)
                description = getattr(ent, "description", None) or (
                    f"Button {channel}" if channel is not None else None
                )

            if channel is None:
                return None

            try:
                return {"channel": int(channel), "button_name": description}
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

    # Register the callback in the platform callbacks registry
    platform_callbacks = enocean_data.get("platform_callbacks", {})
    platform_callbacks["button"] = _add_buttons_from_eep


class EnOceanButton(EnOceanEntity, ButtonEntity):
    """Representation of an EnOcean button device."""

    def __init__(
        self,
        dev_id: list[int],
        dev_name: str,
        channel: int,
        button_name: str,
        fields: EEPEntityDef | None = None,
    ) -> None:
        """Initialize the EnOcean button device."""
        super().__init__(
            dev_id,
            data_field=f"{button_name}_{channel}",
            attr_name=button_name,
            dev_name=dev_name,
            fields=fields,
        )
        self.channel = channel
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
        dev_name: str,
        channel: int,
        button_name: str,
        rorg: int,
        rorg_func: int,
        rorg_type: int,
        fields: EEPEntityDef | None = None,
    ) -> None:
        """Initialize the dynamic EnOcean button device."""
        super().__init__(
            dev_id=dev_id,
            dev_name=dev_name,
            data_field=f"{button_name}_{channel}",
            rorg=rorg,
            rorg_func=rorg_func,
            rorg_type=rorg_type,
            attr_name=button_name,
            fields=fields,
        )
        # Initialize button-specific attributes
        self.channel = channel
        self._attr_name = f"{dev_name} {button_name}"
