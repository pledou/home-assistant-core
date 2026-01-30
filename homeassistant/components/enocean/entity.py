"""Representation of an EnOcean device."""

from enocean.protocol.eep_metadata import load_eep_fields
from enocean.protocol.packet import Packet
from enocean.protocol.parser import Parser

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, dispatcher_send
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, LOGGER, SIGNAL_RECEIVE_MESSAGE, SIGNAL_SEND_MESSAGE
from .types import EEPEntityDef


class EnOceanEntity(Entity):
    """Parent class for all entities associated with the EnOcean component."""

    def __init__(
        self,
        dev_id: list[int],
        data_field: str,
        attr_name: str | None = None,
        dev_name: str | None = None,
        dev_class: str | None = None,
    ) -> None:
        """Initialize the device."""
        self.dev_id = dev_id
        # Use attribute name for the entity short name and let Home Assistant
        # compose the full display name from the entity name + device name by
        # enabling `has_entity_name` on the entity.
        self._attr_has_entity_name = True
        self._attr_name = attr_name or data_field
        self._attr_unique_id = f"{format_device_id_hex_underscore(self.dev_id)}-{data_field.lower().replace(' ', '_')}"
        # Store device display name separately and expose via device_info
        self._device_name = dev_name or f"EnOcean {format_device_id_hex(self.dev_id)}"
        self._data_field = data_field
        # Use standard attribute for device/entity class when provided
        if dev_class is not None:
            self._attr_device_class = dev_class

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for this entity."""
        return DeviceInfo(
            identifiers={(DOMAIN, format_device_id_hex_underscore(self.dev_id))},
            name=getattr(self, "_device_name", None),
        )

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_RECEIVE_MESSAGE, self._message_received_callback
            )
        )

    def _message_received_callback(self, packet):
        """Handle incoming packets."""

        # Compare packet sender integer to device id converted to integer
        try:
            sender_int_expected = int.from_bytes(bytes(self.dev_id), "big")
        except (TypeError, ValueError):
            # Fallback: if dev_id is already an int-like string, try to coerce
            try:
                sender_int_expected = int(str(self.dev_id))
            except (ValueError, TypeError):
                return

        if packet.sender_int == sender_int_expected:
            self.value_changed(packet)

    def value_changed(self, packet):
        """Update the internal state of the device when a packet arrives."""

    def send_command(self, data, optional, packet_type):
        """Send a command via the EnOcean dongle."""

        packet = Packet(packet_type, data=data, optional=optional)
        dispatcher_send(self.hass, SIGNAL_SEND_MESSAGE, packet)


def format_device_id_hex(dev_id: list[int]) -> str:
    """Return colon-separated hex string for a device id list.

    Example: [0x01,0x02,0x03,0x04] -> "01:02:03:04"
    """
    return ":".join(f"{byte:02x}" for byte in dev_id)


def format_device_id_hex_underscore(dev_id: list[int]) -> str:
    """Return underscore-separated hex string for a device id list.

    Example: [0x01,0x02,0x03,0x04] -> "01_02_03_04"
    """
    return "_".join(f"{byte:02x}" for byte in dev_id)


class DynamicEnoceanEntity(EnOceanEntity):
    """Base class for dynamic EnOcean entities that use EEP parser.

    Provides lazy parser initialization, command matching and parsing
    helpers so platform implementations can reuse the logic.
    """

    def __init__(
        self,
        dev_id: list[int],
        data_field: str,
        rorg: int,
        rorg_func: int,
        rorg_type: int,
        dev_name: str | None = None,
        dev_class: str | None = None,
        attr_name: str | None = None,
        command: int | None = None,
        fields: EEPEntityDef | None = None,
    ) -> None:
        """Initialize the dynamic EnOcean entity and store parser parameters for lazy initialization."""
        # Call EnOceanEntity initializer directly to avoid MRO issues
        # where super() would resolve to a concrete sensor's __init__
        # that requires additional positional arguments.
        EnOceanEntity.__init__(
            self,
            dev_id=dev_id,
            data_field=data_field,
            attr_name=attr_name,
            dev_name=dev_name,
            dev_class=dev_class,
        )

        # Parser parameters
        self._rorg = rorg
        self._rorg_func = rorg_func
        self._rorg_type = rorg_type
        self._parser: object | None = None
        self._parser_initialized = False

        try:
            self._command = int(str(command), 0) if command is not None else None
        except (ValueError, TypeError):
            self._command = None

        self._fields = fields

    def _ensure_parser(self) -> None:
        """Ensure the EEP Parser is initialized (lazy)."""
        if self._parser_initialized:
            return
        try:
            self._parser = Parser(
                rorg=self._rorg, func=self._rorg_func, type_=self._rorg_type
            )
        except (FileNotFoundError, OSError, ValueError) as err:
            LOGGER.debug(
                "Failed to initialize EEP parser for %s: %s",
                self._attr_unique_id,
                err,
            )
            self._parser = None
        finally:
            self._parser_initialized = True

    def _packet_matches_command(self, packet) -> bool:
        """Return True if packet matches registered command or if no command set."""
        if self._command is None:
            return True
        if not getattr(packet, "data", None) or len(packet.data) < 2:
            return False
        pkt_cmd = (packet.data[1] & 0xF0) >> 4
        return pkt_cmd == self._command

    def _parse_packet(self, packet):
        """Parse packet using the initialized parser and registered command.

        Returns parsed mapping or None on failure.
        """
        self._ensure_parser()
        if self._parser is None:
            return None
        try:
            return self._parser.parse_packet(packet.data, command=self._command)
        except (ValueError, TypeError, KeyError, IndexError, OSError) as err:
            LOGGER.debug(
                "Failed to parse packet for %s: %s",
                self._attr_unique_id,
                err,
            )
            return None


async def async_create_entities_from_eep(
    hass: HomeAssistant,
    config_entry,
    device_id: list[int],
    device_id_hex: str,
    entities_list: list[EEPEntityDef] | None,
    rorg: int,
    rorg_func: int,
    rorg_type: int,
    platform_type: str,
    entity_class,
    async_add_entities,
    entity_kwargs_factory=None,
) -> None:
    """Factory function to create entities from EEP definitions.

    This shared function handles the common logic for all entity platforms:
    - Filtering entities by type
    - Generating consistent unique IDs
    - Avoiding duplicates
    - Loading EEP fields
    - Creating and adding entities

    Args:
        hass: Home Assistant instance
        config_entry: Config entry
        device_id: 4-byte device ID list
        device_id_hex: Hex string representation of device ID (colon-separated)
        entities_list: List of EEPEntityDef objects
        rorg, func, type_: EEP profile identifiers
        platform_type: Entity platform ("sensor", "binary_sensor", etc.)
        entity_class: The entity class to instantiate
        async_add_entities: Callback to add entities
        entity_kwargs_factory: Optional callable(ent, device_id, device_id_hex, device_name, rorg_int, func_int, type_int, description) -> dict of extra kwargs
    """

    if not entities_list:
        return

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_device(
        identifiers={("enocean", device_id_hex)}
    )
    if not device_entry or config_entry.entry_id not in device_entry.config_entries:
        return

    device_name = device_entry.name or f"enocean {device_id_hex}"
    new_entities = []
    entity_registry = er.async_get(hass)

    for ent in entities_list:
        try:
            # Filter by entity type
            if getattr(ent, "entity_type", "sensor") != platform_type:
                continue

            # Generate consistent entity ID
            unique_suffix = (
                (ent.data_field or ent.name or "entity").lower().replace(" ", "_")
            )
            device_hex_underscore = format_device_id_hex_underscore(device_id)
            unique_id = f"{device_hex_underscore}_{unique_suffix}"

            # Skip if entity already exists
            if entity_registry.async_get_entity_id(platform_type, "enocean", unique_id):
                LOGGER.debug(
                    "%s entity %s for device %s already exists, skipping",
                    platform_type.capitalize(),
                    unique_id,
                    device_id_hex,
                )
                continue

            LOGGER.debug(
                "Creating %s for device %s: data_field=%s, name=%s",
                platform_type,
                device_id_hex,
                ent.data_field,
                ent.name,
            )

            # Create description if provided
            description = None
            if hasattr(ent, "description") and ent.description:
                description = ent.description

            # Load EEP fields
            fields = await hass.async_add_executor_job(
                load_eep_fields,
                f"0x{rorg:X}",
                f"0x{rorg_func:02X}",
                f"0x{rorg_type:02X}",
            )

            # Build entity kwargs
            entity_kwargs = {
                "data_field": ent.data_field,
                "device_class": ent.device_class,
                "rorg": rorg,
                "rorg_func": rorg_func,
                "rorg_type": rorg_type,
                "command": getattr(ent, "command", None),
                "fields": fields,
            }

            if description:
                entity_kwargs["description"] = description

            # Add platform-specific kwargs
            if entity_kwargs_factory:
                extra_kwargs = entity_kwargs_factory(
                    ent,
                    device_id,
                    device_id_hex,
                    device_name,
                    rorg,
                    rorg_func,
                    rorg_type,
                    description,
                )
                if extra_kwargs:
                    entity_kwargs.update(extra_kwargs)

            # Create the entity
            new_entities.append(
                entity_class(
                    device_id,
                    device_id_hex,
                    device_name,
                    **entity_kwargs,
                )
            )

        except (FileNotFoundError, OSError, ValueError, TypeError) as err:
            LOGGER.exception(
                "Failed to create %s entity from EEP def: %s, error: %s",
                platform_type,
                ent,
                err,
            )

    if new_entities:
        async_add_entities(new_entities)
