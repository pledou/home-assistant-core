"""Support for EnOcean select entities derived from EEP enums.

Select entities are created dynamically from discovery using EEP enum
definitions. The dispatcher receives (device_id, entities) where `entities`
are `EEPEntityDef`-like objects produced by the EEP loader.
"""

from __future__ import annotations

from typing import Any

from enocean.protocol.eep_metadata import get_field_value_with_enum

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SIGNAL_ADD_ENTITIES
from .const import LOGGER
from .entity import DynamicEnoceanEntity, EnOceanEntity, async_create_entities_from_eep
from .types import EEPEntityDef


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EnOcean select entities."""
    # Selects are created dynamically from discovery events

    async def _add_selects_from_eep(device_id, entities_list, rorg, func, type_):
        """Add select entities for a discovered device from EEP profile."""
        await async_create_entities_from_eep(
            hass,
            config_entry,
            device_id,
            entities_list,
            rorg,
            func,
            type_,
            platform_type="select",
            entity_class=DynamicEnOceanSelect,
            async_add_entities=async_add_entities,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_ADD_ENTITIES, _add_selects_from_eep)
    )


class EnOceanSelect(EnOceanEntity, SelectEntity):
    """Representation of an EnOcean select entity (EEP enum)."""

    def __init__(
        self,
        dev_id: list[int],
        dev_name: str,
        data_field: str | None = None,
        options: list[str] | None = None,
        entity_name: str | None = None,
    ) -> None:
        """Initialize the select entity."""
        EnOceanEntity.__init__(
            self,
            dev_id,
            data_field=data_field or dev_name,
            dev_name=dev_name,
            dev_class=None,
            attr_name=entity_name,
        )
        self._data_field: str = data_field or dev_name
        self._attr_options = options or []
        self._current_option: str | None = None

    @property
    def current_option(self) -> str | None:
        """Return currently selected option."""
        return self._current_option

    async def async_select_option(self, option: str) -> None:
        """Select an option.

        NOTE: This implementation only updates local state. Sending the
        selection back to the device requires composing and sending the
        appropriate EEP command; that can be implemented later when a
        command mapping is available.
        """
        if option not in self._attr_options:
            return
        self._current_option = option
        self.async_write_ha_state()

    def value_changed(self, packet: Any) -> None:
        """Update current option based on incoming packet.

        Try to pick a meaningful value from `packet.parsed` when available.
        """
        # If packet provides parsed values, try to extract the field
        parsed = getattr(packet, "parsed", None)
        if parsed and self._data_field:
            try:
                entry = parsed.get(self._data_field)
                if isinstance(entry, dict):
                    raw = (
                        entry.get("raw_value") or entry.get("value") or entry.get("raw")
                    )
                else:
                    raw = entry

                # Match raw value to options by description or numeric value
                if raw is not None:
                    raw_str = str(raw)
                    for opt in self._attr_options:
                        if opt == raw_str or opt.lower() == raw_str.lower():
                            self._current_option = opt
                            self.schedule_update_ha_state()
                            return
            except (AttributeError, KeyError, TypeError, ValueError) as err:
                LOGGER.debug(
                    "Failed to map packet to select option for %s: error: %s",
                    self._attr_unique_id,
                    err,
                )


class DynamicEnOceanSelect(DynamicEnoceanEntity, EnOceanSelect):
    """Generic dynamic select that parses EEP enums using Parser.

    This select can be configured per-instance with explicit EEP
    identifiers (rorg/func/type) and an optional fields mapping.
    """

    def __init__(
        self,
        dev_id: list[int],
        rorg: int,
        rorg_func: int,
        rorg_type: int,
        dev_name: str,
        data_field: str,
        device_class: str | None = None,
        fields: EEPEntityDef | None = None,
        command: int | None = None,
    ) -> None:
        """Initialize the dynamic select entity."""
        # Initialize shared dynamic behaviour then set device-specific attrs
        options = getattr(fields, "enum_options", None)

        DynamicEnoceanEntity.__init__(
            self,
            dev_id,
            dev_name=dev_name,
            data_field=data_field,
            rorg=rorg,
            rorg_func=rorg_func,
            rorg_type=rorg_type,
            command=command,
            dev_class=device_class,
            fields=fields,
        )
        EnOceanSelect.__init__(
            self,
            dev_id,
            dev_name=dev_name,
            data_field=data_field,
            options=options,
        )

    def value_changed(self, packet: Any) -> None:
        """Update current option based on incoming packet using parser when available."""
        # Prefer packet.parsed if present
        parsed = getattr(packet, "parsed", None)
        if parsed and self._data_field:
            try:
                entry = parsed.get(self._data_field)
                if isinstance(entry, dict):
                    raw = (
                        entry.get("raw_value") or entry.get("value") or entry.get("raw")
                    )
                else:
                    raw = entry

                if raw is not None:
                    raw_str = str(raw)
                    for opt in self._attr_options:
                        if opt == raw_str or opt.lower() == raw_str.lower():
                            self._current_option = opt
                            self.schedule_update_ha_state()
                            return
            except (AttributeError, KeyError, TypeError, ValueError) as err:
                LOGGER.debug(
                    "Failed to map packet to select option for %s: error: %s",
                    self._attr_unique_id,
                    err,
                )

        # Fallback: use parser to parse raw packet if available
        if not packet.data or len(packet.data) < 2:
            return

        if not self._packet_matches_command(packet):
            return

        parsed = self._parse_packet(packet)
        if not parsed or not self._data_field:
            return

        if self._fields and get_field_value_with_enum is not None:
            value = get_field_value_with_enum(parsed, self._data_field, self._fields)
        else:
            value = parsed.get(self._data_field)

        if value is not None:
            raw_str = str(value)
            for opt in self._attr_options:
                if opt == raw_str or opt.lower() == raw_str.lower():
                    self._current_option = opt
                    self.schedule_update_ha_state()
                    return
