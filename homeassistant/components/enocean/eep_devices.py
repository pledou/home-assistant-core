"""EEP device entity definitions with dynamic EEP-based discovery.

Simple, clean architecture:
- Load EEP profile and YAML mapping once per device
- Single public function: get_entities_for_device(eep_profile)
- Returns list of EEPEntityDef ready for entity creation
"""

from __future__ import annotations

import contextlib
from functools import cache
import importlib
import logging
from pathlib import Path

import yaml

from .types import EEPEntityDef, EepProfile, EntityType

_LOGGER = logging.getLogger(__name__)


@cache
def _load_eep_mapping() -> dict:
    """Load EEP-to-platform mapping from YAML file.

    Returns: Dict with structure {rorg: {func: {type: {entities: [...]}}}}
    """
    if yaml is None:
        _LOGGER.warning("PyYAML not available; EEP mapping will not be loaded")
        return {}

    mapping_path = Path(__file__).parent / "eep_platform_mapping.yaml"
    if not mapping_path.exists():
        _LOGGER.warning("EEP mapping file not found at %s", mapping_path)
        return {}

    try:
        with open(mapping_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            mapping = {
                k: v
                for k, v in data.items()
                if k not in ["auto_detect_remove", "common", "system"]
            }
            _LOGGER.debug("Loaded EEP mapping with %d RORG profiles", len(mapping))
            return mapping
    except OSError as err:
        _LOGGER.error("Failed to load EEP mapping file: %s", err)
        return {}
    except yaml.YAMLError as err:
        _LOGGER.error("Failed to parse EEP mapping YAML: %s", err)
        return {}


def _load_eep_profile(eep_profile: EepProfile):
    """Load EEP profile from python-enocean library.

    Args:
        eep_profile: EepProfile with rorg, func, type, manufacturer

    Returns: Parsed EEP profile object or None if not found.
    """
    try:
        eep_module = importlib.import_module("enocean.protocol.eep")
        eep_instance = getattr(eep_module, "_eep_instance", None)
        if eep_instance is None or not getattr(eep_instance, "init_ok", False):
            _LOGGER.debug("EEP parser not initialized")
            profile = None
        else:
            profile = eep_instance.find_profile(
                eep_profile.get("manufacturer"),
                eep_profile.get("rorg"),
                eep_profile.get("rorg_func"),
                eep_profile.get("rorg_type"),
            )
            if profile is None:
                _LOGGER.debug(
                    "EEP profile not found for RORG=%s FUNC=%s TYPE=%s",
                    hex(int(eep_profile.get("rorg") or 0)),
                    hex(int(eep_profile.get("rorg_func") or 0)),
                    hex(int(eep_profile.get("rorg_type") or 0)),
                )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to load EEP profile: %s", err)
        return None
    else:
        return profile


def _extract_eep_fields(
    profile, rorg: int, rorg_func: int, rorg_type: int
) -> list[EEPEntityDef]:
    """Extract field definitions from EEP profile object.

    Args:
        profile: BeautifulSoup element representing an EEP profile
        rorg: RORG value of the EEP profile
        rorg_func: RORG function value of the EEP profile
        rorg_type: RORG type value of the EEP profile

    Returns: List of field dicts with shortcut, description, type, unit, min/max, items.
    """
    fields = []

    # Check for multi-command profiles (contain <data command="..."> elements)
    # If the profile itself is a <data> element, we need to get its siblings
    if profile.name == "data" and profile.parent:
        # Profile is already a <data> element, get all sibling <data> elements from parent
        data_commands = profile.parent.find_all("data")
    else:
        # Profile is a <profile> element, find nested <data> elements
        data_commands = profile.find_all("data")

    containers = data_commands if data_commands else [profile]

    for container in containers:  # pylint: disable=too-many-nested-blocks
        # Extract value, enum, and boolean fields
        for field_type in ("value", "enum", "boolean"):
            for element in container.find_all(field_type, recursive=False):
                shortcut = element.get("shortcut")
                if not shortcut or shortcut == "CMD":
                    continue

                description = element.get("description") or shortcut
                unit = element.get("unit")

                # Extract min/max from <scale> (preferred) or <range>
                min_value = None
                max_value = None

                scale_el = element.find("scale")
                if scale_el:
                    for bound_type, val_var in (
                        ("min", "min_value"),
                        ("max", "max_value"),
                    ):
                        bound_el = scale_el.find(bound_type)
                        if bound_el and bound_el.text:
                            with contextlib.suppress(ValueError, TypeError):
                                locals()[val_var] = float(bound_el.text.strip())

                if min_value is None or max_value is None:
                    range_el = element.find("range")
                    if range_el:
                        for bound_type in ("min", "max"):
                            bound_el = range_el.find(bound_type)
                            if bound_el and bound_el.text:
                                try:
                                    val = float(bound_el.text.strip())
                                    if bound_type == "min" and min_value is None:
                                        min_value = val
                                    elif bound_type == "max" and max_value is None:
                                        max_value = val
                                except (ValueError, TypeError):
                                    pass

                # Extract enum items
                items = None
                if field_type == "enum":
                    item_list = []
                    for item in element.find_all("item"):
                        raw_val = item.get("value")
                        try:
                            val = int(str(raw_val), 0)
                        except (ValueError, TypeError):
                            val = raw_val
                        item_list.append(
                            {"value": val, "description": item.get("description", "")}
                        )
                    if item_list:
                        items = item_list

                offset = None
                offset_el = element.find("offset")
                if offset_el and offset_el.text:
                    with contextlib.suppress(ValueError, TypeError):
                        offset = int(offset_el.text.strip(), 0)
                size = None
                size_el = element.find("size")
                if size_el and size_el.text:
                    with contextlib.suppress(ValueError, TypeError):
                        size = int(size_el.text.strip(), 0)
                fields.append(
                    EEPEntityDef(
                        description=description,
                        rorg=rorg,
                        rorg_func=rorg_func,
                        rorg_type=rorg_type,
                        data_field=shortcut,
                        unit=unit,
                        device_class=None,
                        entity_type=_classify_entity_type(
                            shortcut, description, field_type, items
                        ),
                        min_value=min_value,
                        max_value=max_value,
                        enum_options=(
                            [it["description"] for it in (items or [])]
                            if items
                            else None
                        ),
                        offset=offset,
                    )
                )

    return fields


def _classify_entity_type(
    shortcut: str | None,
    description: str | None,
    field_type: str,
    items: list[dict] | None,
) -> EntityType:
    """Classify field to Home Assistant entity type.

    Returns one of: EntityType.SENSOR, EntityType.BINARY_SENSOR, EntityType.SELECT, EntityType.LIGHT, EntityType.BUTTON.
    """

    sc = (shortcut or "").upper()
    desc = (description or "").lower()

    # Booleans -> binary_sensor
    if field_type == "boolean":
        return EntityType.BINARY_SENSOR

    # Enums: detect binary, rocker, or multi-select
    if field_type == "enum" and items:
        vals = {int(it["value"]) if isinstance(it["value"], int) else 0 for it in items}
        if vals == {0, 1}:
            return EntityType.BINARY_SENSOR
        if sc.startswith("R") and len(vals) >= 3:
            return EntityType.BUTTON
        if len(vals) > 2:
            return EntityType.SELECT
        return EntityType.SENSOR

    # Known shortcuts -> binary_sensor
    if sc in {"WAS", "SMO", "CO"}:
        return EntityType.BINARY_SENSOR

    if sc == "WIN":
        return EntityType.SELECT

    # Lights/dimmers
    if any(k in sc for k in ("EDIM", "DIM", "BRI", "BRIGHT", "DMD")) or any(
        w in desc for w in ("dimm", "brightness")
    ):
        return EntityType.LIGHT

    # Commands -> button
    if sc == "CMD" or "command" in desc:
        return EntityType.BUTTON

    # Default: sensor
    return EntityType.SENSOR


def get_entities_for_device(eep_profile: EepProfile) -> list[EEPEntityDef]:
    """Build entity definitions for a device from EEP profile, overlaid with YAML mapping.

    This is the single public API function. It handles:
    1. Load EEP profile (source of truth for all available fields)
    2. Build entities from EEP
    3. Overlay YAML mapping overrides for customization
    4. Return merged list

    Args:
        eep_profile: EepProfile with rorg, func, type, manufacturer

    Returns: List of EEPEntityDef instances for entity creation.
    """
    rorg = eep_profile["rorg"]
    rorg_func = eep_profile["rorg_func"]
    rorg_type = eep_profile["rorg_type"]

    if any(v is None for v in (rorg, rorg_func, rorg_type)):
        _LOGGER.warning("Invalid EEP profile: missing rorg, func, or type")
        return []

    # Load EEP profile (source of truth)
    profile = _load_eep_profile(eep_profile)
    if not profile:
        _LOGGER.warning(
            "No EEP profile found for RORG=%s FUNC=%s TYPE=%s",
            hex(rorg),
            hex(rorg_func),
            hex(rorg_type),
        )
        return []

    eep_fields = _extract_eep_fields(profile, rorg, rorg_func, rorg_type)
    if not eep_fields:
        _LOGGER.warning(
            "EEP profile has no fields for RORG=%s FUNC=%s TYPE=%s",
            hex(rorg),
            hex(rorg_func),
            hex(rorg_type),
        )
        return []

    entities = eep_fields

    # Overlay YAML mapping overrides
    mapping = _load_eep_mapping()
    rorg_entry = mapping.get(rorg)
    if rorg_entry:
        func_entry = rorg_entry.get(rorg_func)
        if func_entry:
            type_entry = func_entry.get(rorg_type)
            if type_entry and type_entry.get("entities"):
                entities = _overlay_mapping_overrides(entities, type_entry)

    _LOGGER.debug(
        "Built %d entities from EEP profile with mapping overrides (RORG=%s FUNC=%s TYPE=%s)",
        len(entities),
        hex(rorg),
        hex(rorg_func),
        hex(rorg_type),
    )

    return entities


def _overlay_mapping_overrides(
    eep_entities: list[EEPEntityDef], type_entry: dict
) -> list[EEPEntityDef]:
    """Overlay YAML mapping overrides onto EEP-derived entities.

    For each mapping entity, find the matching EEP entity by data_field name
    and update its properties with mapping values.

    Args:
        eep_entities: List of entities from EEP extraction
        type_entry: Mapping type_entry dict with 'entities' list

    Returns: Updated entities list with mapping overrides applied.
    """
    # Build lookup of mapping entities by name (data_field)
    mapping_lookup = {}
    for entity_def in type_entry.get("entities", []):
        name = entity_def.get("name")
        if name:
            mapping_lookup[name] = entity_def

    # Apply overrides to matching EEP entities
    for eep_entity in eep_entities:
        data_field = eep_entity.data_field
        if data_field in mapping_lookup:
            mapping_def = mapping_lookup[data_field]
            config = mapping_def.get("config", {})

            _LOGGER.debug(
                "Applying mapping override for %s: component=%s -> %s",
                data_field,
                mapping_def.get("component"),
                eep_entity.entity_type,
            )

            # Apply mapping overrides
            if mapping_def.get("component"):
                component_str = mapping_def["component"]
                # Convert string component to EntityType enum
                try:
                    old_type = eep_entity.entity_type
                    eep_entity.entity_type = EntityType(component_str)
                    _LOGGER.debug(
                        "Overrode entity type for %s from %s to %s",
                        data_field,
                        old_type,
                        eep_entity.entity_type,
                    )
                except ValueError:
                    _LOGGER.warning(
                        "Unknown component type '%s' in mapping for %s, keeping auto-classified %s",
                        component_str,
                        data_field,
                        eep_entity.entity_type,
                    )

            if config.get("unit_of_measurement"):
                eep_entity.unit = config["unit_of_measurement"]

            if config.get("device_class"):
                eep_entity.device_class = config["device_class"]

            if config.get("min") is not None:
                with contextlib.suppress(ValueError, TypeError):
                    eep_entity.min_value = float(config["min"])

            if config.get("max") is not None:
                with contextlib.suppress(ValueError, TypeError):
                    eep_entity.max_value = float(config["max"])

            if config.get("options"):
                eep_entity.enum_options = config["options"]

    return eep_entities
