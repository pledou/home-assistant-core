"""Representation of an EnOcean device."""

import contextlib
import inspect
from typing import Any

from enocean.protocol.eep_metadata import load_eep_fields
from enocean.protocol.packet import Packet

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, dispatcher_send
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, LOGGER, SIGNAL_RECEIVE_MESSAGE, SIGNAL_SEND_MESSAGE
from .types import EEPEntityDef, EntityType


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

    @callback
    def _message_received_callback(self, packet):
        """Handle incoming packets."""

        # Skip teach-in packets (RORG 0xD4) - they don't contain sensor data
        if hasattr(packet, "rorg") and packet.rorg == 0xD4:
            return

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

    @callback
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
    """Base class for dynamic EnOcean entities that use pre-parsed packet data.

    This class expects packets to be pre-parsed by the dongle's callback.
    Entities simply read from packet.parsed instead of doing their own parsing.
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
        fields: EEPEntityDef | None = None,
    ) -> None:
        """Initialize the dynamic EnOcean entity."""
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

        # Store EEP profile info for reference (parsing is done by dongle)
        self._rorg = rorg
        self._rorg_func = rorg_func
        self._rorg_type = rorg_type
        self._fields = fields

        # Apply common properties from EEPEntityDef to entity attributes
        if fields is not None and isinstance(fields, EEPEntityDef):
            if fields.icon:
                self._attr_icon = fields.icon

            if fields.entity_category:
                self._attr_entity_category = fields.entity_category  # type: ignore[assignment]

    def _get_parsed_value(self, packet, field_name: str):
        """Get a field value from the pre-parsed packet data.

        The packet should already be parsed by the dongle's callback.
        This method simply extracts the requested field from packet.parsed.

        Args:
            packet: EnOcean packet with parsed data
            field_name: Name of the field to extract

        Returns:
            Field value or None if not found
        """
        if not packet.parsed:
            LOGGER.debug(
                "Packet for %s has no parsed data - ensure device profile is registered",
                self._attr_unique_id,
            )
            return None

        try:
            # Handle nested dict structure (e.g., {"FIELD": {"raw_value": 123}})
            field_data = packet.parsed.get(field_name)
        except (KeyError, AttributeError, TypeError) as err:
            LOGGER.debug(
                "Failed to extract field %s for %s: %s",
                field_name,
                self._attr_unique_id,
                err,
            )
            return None
        else:
            if isinstance(field_data, dict):
                return field_data.get("raw_value", field_data.get("value"))
            return field_data


def _build_eep_fields_obj(
    hass: HomeAssistant,
    ent,
    fields,
    rorg: int,
    rorg_func: int,
    rorg_type: int,
    unique_id: str,
) -> EEPEntityDef | None:
    """Build EEPEntityDef from loaded EEP fields metadata.

    Converts fields returned by load_eep_fields into an EEPEntityDef dataclass
    for consistent access to min_value, max_value, unit across different formats.
    This function is synchronous since it only manipulates in-memory data
    returned by `load_eep_fields` which is executed in the executor.
    """
    fields_obj = None
    try:
        if not fields:
            return None

        # Attempt to locate metadata for the specific data_field
        # Expect `fields` to be an object-like metadata container; use
        # attribute access to obtain the metadata for the requested field.
        meta = getattr(fields, ent.data_field, None)

        # Extract min/max/unit values based on meta type
        min_v, max_v, unit_v, enum_opts, offset_v = _extract_field_metadata(meta)

        # Normalize entity_type
        entity_type = _normalize_entity_type(ent)

        fields_obj = EEPEntityDef(
            description=ent.description,
            rorg=rorg,
            rorg_func=rorg_func,
            rorg_type=rorg_type,
            data_field=ent.data_field,
            entity_type=entity_type,
            unit=unit_v or ent.unit,
            device_class=ent.device_class,
            min_value=(None if min_v is None else float(min_v)),
            max_value=(None if max_v is None else float(max_v)),
            enum_options=enum_opts or ent.enum_options,
            offset=offset_v,
        )
        # Attach original raw fields mapping to the dataclass instance
        # so callers that need the full EEP mapping (dict) can access it
        # via `raw_fields` when only the dataclass is provided.
        with contextlib.suppress(Exception):
            setattr(fields_obj, "raw_fields", fields)
    except (AttributeError, KeyError, TypeError, ValueError) as err:
        # Log at debug level and leave fields_obj as None if metadata
        # extraction or conversion fails
        LOGGER.debug(
            "Failed to build fields_obj for %s: %s",
            unique_id,
            err,
        )
        fields_obj = None

    return fields_obj


def _extract_field_metadata(meta):
    """Extract min_v, max_v, unit_v, enum_opts, offset_v from metadata."""
    min_v = max_v = unit_v = enum_opts = offset_v = None

    if meta and isinstance(meta, dict):
        min_v = meta.get("min_value") or meta.get("min") or meta.get("minimum")
        max_v = meta.get("max_value") or meta.get("max") or meta.get("maximum")
        unit_v = meta.get("unit") or meta.get("units")
        enum_opts = meta.get("enum_options") or meta.get("enum")
        offset_v = meta.get("offset")
    elif meta:
        # If meta is not a dict, try attribute names
        min_v = getattr(meta, "min_value", None)
        max_v = getattr(meta, "max_value", None)
        unit_v = getattr(meta, "unit", None)
        enum_opts = getattr(meta, "enum_options", None)
        offset_v = getattr(meta, "offset", None)

    return min_v, max_v, unit_v, enum_opts, offset_v


def _normalize_entity_type(ent) -> EntityType:
    """Normalize entity_type to EntityType enum."""
    entity_type = getattr(ent, "entity_type", EntityType.SENSOR)
    if entity_type is None:
        entity_type = EntityType.SENSOR
    elif isinstance(entity_type, str):
        # Convert string to EntityType enum
        try:
            entity_type = EntityType(entity_type)
        except ValueError:
            entity_type = EntityType.SENSOR

    return entity_type


async def async_create_entities_from_eep(
    hass: HomeAssistant,
    config_entry,
    device_id: list[int],
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
        entities_list: List of EEPEntityDef objects
        rorg, func, type_: EEP profile identifiers
        platform_type: Entity platform ("sensor", "binary_sensor", etc.)
        entity_class: The entity class to instantiate
        async_add_entities: Callback to add entities
        entity_kwargs_factory: Optional callable(ent, device_id, device_name, rorg_int, func_int, type_int, description) -> dict of extra kwargs
    """

    if not entities_list:
        LOGGER.debug(
            "No entities provided for platform %s, device %s",
            platform_type,
            format_device_id_hex(device_id),
        )
        return

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_device(
        identifiers={("enocean", format_device_id_hex_underscore(device_id))}
    )
    if not device_entry:
        LOGGER.warning(
            "Device not found in registry for %s when creating %s entities",
            format_device_id_hex(device_id),
            platform_type,
        )
        return
    if config_entry.entry_id not in device_entry.config_entries:
        LOGGER.warning(
            "Config entry %s not in device config entries for %s",
            config_entry.entry_id,
            format_device_id_hex(device_id),
        )
        return

    device_name = device_entry.name or f"enocean {format_device_id_hex(device_id)}"
    new_entities = []
    entity_registry = er.async_get(hass)

    entities_filtered = 0
    for ent in entities_list:
        try:
            # Filter by entity type
            entity_type = getattr(ent, "entity_type", "sensor")
            # Handle EntityType enum by getting its value
            entity_type_str = (
                entity_type.value if hasattr(entity_type, "value") else entity_type
            )
            if entity_type_str != platform_type:
                entities_filtered += 1
                continue

            # Generate consistent entity ID
            unique_suffix = (
                (ent.data_field or ent.description or "entity")
                .lower()
                .replace(" ", "_")
            )
            device_hex_underscore = format_device_id_hex_underscore(device_id)
            unique_id = f"{device_hex_underscore}-{unique_suffix}"
            attr_name = ent.description or ent.data_field or "Entity"

            # Skip if entity already exists
            if entity_registry.async_get_entity_id(platform_type, "enocean", unique_id):
                continue

            fields = await hass.async_add_executor_job(
                load_eep_fields,
                f"0x{rorg:X}",
                f"0x{rorg_func:02X}",
                f"0x{rorg_type:02X}",
            )

            # Build fields_obj from loaded fields (synchronous)
            fields_obj = _build_eep_fields_obj(
                hass, ent, fields, rorg, rorg_func, rorg_type, unique_id
            )

            # Build entity kwargs
            entity_kwargs = {
                "data_field": ent.data_field,
                "device_class": ent.device_class,
                "rorg": rorg,
                "rorg_func": rorg_func,
                "rorg_type": rorg_type,
                "fields": fields_obj or fields,
            }

            if attr_name:
                entity_kwargs["attr_name"] = attr_name

            # Add enum_options if available (for select entities)
            if hasattr(ent, "enum_options") and ent.enum_options:
                entity_kwargs["enum_options"] = ent.enum_options

            # Add platform-specific kwargs
            if entity_kwargs_factory:
                extra_kwargs = entity_kwargs_factory(ent)
                if extra_kwargs:
                    entity_kwargs.update(extra_kwargs)

            entity_kwargs.setdefault("dev_name", device_name)

            # Inspect the target constructor and build positional args
            # for any required positional parameters to avoid passing the
            # same argument both positionally and via kwargs which can
            # raise a TypeError in some subclass __init__ implementations.
            try:
                sig = inspect.signature(entity_class.__init__)
                # parameter list excluding 'self'
                params = list(sig.parameters.values())[1:]
                param_names = [p.name for p in params]
            except (ValueError, TypeError):
                params = []
                param_names = []

            positional_args: list[int | str | EEPEntityDef | Any] = []
            # If the constructor expects a first parameter like 'dev_id',
            # pass the device_id positionally
            if param_names and param_names[0] in ("dev_id"):
                positional_args.append(device_id)

            # For required positional parameters after the first, consume
            # them from entity_kwargs and pass positionally to avoid
            # duplicate-assignment errors
            positional_args.extend(
                entity_kwargs.pop(p.name)
                for p in params[1:]
                if (
                    p.kind
                    in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                    and p.default is inspect.Parameter.empty
                    and p.name in entity_kwargs
                )
            )

            # Filter kwargs to parameters the constructor actually accepts
            filtered_kwargs = {
                k: v
                for k, v in entity_kwargs.items()
                if (not param_names) or (k in param_names)
            }

            # Create the entity using constructed args/kwargs
            entity_obj = entity_class(*positional_args, **filtered_kwargs)
            new_entities.append(entity_obj)

        except (FileNotFoundError, OSError, ValueError, TypeError) as err:
            LOGGER.exception(
                "Failed to create %s entity from EEP def: %s, error: %s",
                platform_type,
                ent,
                err,
            )

    if new_entities:
        async_add_entities(new_entities)
