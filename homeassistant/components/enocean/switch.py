"""Support for EnOcean switches."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ID, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import (
    AddConfigEntryEntitiesCallback,
    AddEntitiesCallback,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import DATA_ENOCEAN, DOMAIN, ENOCEAN_DONGLE, LOGGER
from .dongle import SIGNAL_LEARNING_MODE_CHANGED
from .entity import (
    DynamicEnoceanEntity,
    EnOceanEntity,
    async_create_entities_from_eep,
    format_device_id_hex_underscore,
)
from .types import EEPEntityDef

CONF_CHANNEL = "channel"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EnOcean switch entities."""
    enocean_data = hass.data.get(DATA_ENOCEAN, {})
    dongle = enocean_data.get(ENOCEAN_DONGLE)

    if not dongle:
        return

    entities = [
        EnOceanLearnSwitch(dongle),
    ]

    async_add_entities(entities)

    # Register callback to add switch entities discovered via EEP using shared factory
    async def _add_switches_from_eep(
        device_id, entities_list, rorg, rorg_func, rorg_type
    ):
        """Add switch entities for a discovered device from EEP profile."""

        def _kwargs_factory(
            ent,
            device_id,
            device_name,
            rorg_int,
            func_int,
            type_int,
            description,
        ):
            # Use channel/index from offset if available as fallback
            channel = (
                ent.get("offset")
                if isinstance(ent, dict)
                else getattr(ent, "offset", None)
            )
            if channel is None:
                return None
            try:
                return {"channel": int(channel)}
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
            platform_type="switch",
            entity_class=DynamicEnOceanSwitch,
            async_add_entities=async_add_entities,
            entity_kwargs_factory=_kwargs_factory,
        )

    # Register the callback in the platform callbacks registry
    platform_callbacks = enocean_data.get("platform_callbacks", {})
    platform_callbacks["switch"] = _add_switches_from_eep


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the EnOcean switch platform."""
    channel: int = config[CONF_CHANNEL]
    dev_id: list[int] = config[CONF_ID]
    dev_name: str = config[CONF_NAME]
    async_add_entities(
        [
            EnOceanSwitch(
                dev_id,
                data_field=dev_name,
                attr_name=dev_name,
                dev_name=None,
                channel=channel,
            )
        ]
    )


class EnOceanSwitch(EnOceanEntity, SwitchEntity):
    """Representation of an EnOcean switch device."""

    _attr_is_on = False

    def __init__(
        self,
        dev_id: list[int],
        data_field: str,
        attr_name: str | None = None,
        dev_name: str | None = None,
        channel: int | None = None,
    ) -> None:
        """Initialize the EnOcean switch device."""
        EnOceanEntity.__init__(
            self,
            dev_id,
            data_field=data_field,
            attr_name=attr_name,
            dev_name=dev_name,
            dev_class=None,
        )
        self._light = None
        self.channel = channel
        self._attr_unique_id = f"{format_device_id_hex_underscore(dev_id)}-{channel}"
        self._attr_name = dev_name

    def turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        optional = [0x03]
        optional.extend(self.dev_id)
        optional.extend([0xFF, 0x00])
        self.send_command(
            data=[
                0xD2,
                0x01,
                (self.channel or 0) & 0xFF,
                0x64,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
            ],
            optional=optional,
            packet_type=0x01,
        )
        self._attr_is_on = True

    def turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        optional = [0x03]
        optional.extend(self.dev_id)
        optional.extend([0xFF, 0x00])
        self.send_command(
            data=[
                0xD2,
                0x01,
                (self.channel or 0) & 0xFF,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
            ],
            optional=optional,
            packet_type=0x01,
        )
        self._attr_is_on = False

    def value_changed(self, packet):
        """Update the internal state of the switch."""
        if packet.data[0] == 0xA5:
            # power meter telegram, turn on if > 10 watts
            packet.parse_eep(0x12, 0x01)
            if packet.parsed["DT"]["raw_value"] == 1:
                raw_val = packet.parsed["MR"]["raw_value"]
                divisor = packet.parsed["DIV"]["raw_value"]
                watts = raw_val / (10**divisor)
                if watts > 1:
                    self._attr_is_on = True
                    self.schedule_update_ha_state()
        elif packet.data[0] == 0xD2:
            # actuator status telegram
            packet.parse_eep(0x01, 0x01)
            if packet.parsed["CMD"]["raw_value"] == 4:
                channel = packet.parsed["IO"]["raw_value"]
                output = packet.parsed["OV"]["raw_value"]
                if channel == self.channel:
                    self._attr_is_on = output > 0
                    self.schedule_update_ha_state()


class DynamicEnOceanSwitch(DynamicEnoceanEntity, EnOceanSwitch):
    """Dynamic switch that uses EEP parser/fields when available."""

    def __init__(
        self,
        dev_id: list[int],
        rorg: int,
        rorg_func: int,
        rorg_type: int,
        data_field: str,
        attr_name: str | None = None,
        dev_name: str | None = None,
        channel: int | None = None,
        device_class: str | None = None,
        fields: EEPEntityDef | None = None,
        command: int | None = None,
    ) -> None:
        """Initialize the dynamic EnOcean switch device."""
        # Initialize dynamic base (parser/command/fields) then EnOceanSwitch
        DynamicEnoceanEntity.__init__(
            self,
            dev_id,
            data_field=data_field,
            rorg=rorg,
            rorg_func=rorg_func,
            rorg_type=rorg_type,
            dev_name=dev_name,
            dev_class=device_class,
            command=command,
            fields=fields,
        )
        EnOceanSwitch.__init__(
            self,
            dev_id,
            data_field=data_field,
            attr_name=attr_name,
            dev_name=dev_name,
            channel=channel,
        )

    def value_changed(self, packet):
        """Prefer parsed values via parser/fields, fallback to base implementation."""
        if not packet.data or len(packet.data) < 2:
            return

        # Ensure packet matches configured command
        if not self._packet_matches_command(packet):
            return

        try:
            parsed = self._parse_packet(packet)
            if parsed and self._fields:
                # Try to extract channel and output fields commonly used
                ch = parsed.get("IO") or parsed.get("CH") or parsed.get("IO_NUM")
                out = parsed.get("OV") or parsed.get("OUT") or parsed.get("OUTPUT")
                if isinstance(ch, dict):
                    ch = ch.get("raw_value") or ch.get("value")
                if isinstance(out, dict):
                    out = out.get("raw_value") or out.get("value")
                if ch is not None and out is not None:
                    try:
                        if int(ch) == int(self.channel):
                            self._attr_is_on = int(out) > 0
                            self.schedule_update_ha_state()
                            return
                    except (ValueError, TypeError):
                        pass
        except (ValueError, TypeError, KeyError) as err:
            LOGGER.debug(
                "Parser failed for dynamic switch %s: %s", self._attr_unique_id, err
            )

        # Fallback to original logic
        super().value_changed(packet)


class EnOceanLearnSwitch(SwitchEntity):
    """Representation of an EnOcean learn mode switch."""

    _attr_has_entity_name = True
    _attr_is_on = False

    def __init__(self, dongle) -> None:
        """Initialize the learn mode switch."""
        self._dongle = dongle
        self._attr_unique_id = f"{dongle.identifier}-learn"
        self._attr_name = "Learning mode"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the dongle."""
        return {
            "identifiers": {(DOMAIN, self._dongle.identifier)},
            "name": f"EnOcean Dongle ({self._dongle.identifier})",
            "manufacturer": "EnOcean",
        }

    async def async_added_to_hass(self) -> None:
        """Register callback for learning mode changes."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_LEARNING_MODE_CHANGED,
                self._learning_mode_changed_callback,
            )
        )
        # Set initial state
        self._attr_is_on = self._dongle.learning_mode
        self.async_write_ha_state()

    async def _learning_mode_changed_callback(self, data: dict) -> None:
        """Handle learning mode changes."""
        self._attr_is_on = data.get("enabled", False)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on learning mode."""
        await self._dongle.async_start_learning()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off learning mode."""
        await self._dongle.async_stop_learning()
