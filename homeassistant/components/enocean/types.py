"""Typed definitions for the EnOcean integration.

Keep small, import-safe TypedDicts here so other modules can import
them without causing circular package imports.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TypedDict


class EepProfile(TypedDict):
    """TypedDict describing an EEP profile for discovery signals."""

    rorg: int
    rorg_func: int
    rorg_type: int
    manufacturer: int | None


class DiscoveryInfo(TypedDict):
    """TypedDict for the discovery info dispatched for new devices."""

    device_id: list[int]
    eep_profile: EepProfile


class EntityType(Enum):
    """Enum describing entity types supported by the EnOcean integration.

    Values:
        SENSOR: Regular sensor entity
        BINARY_SENSOR: Binary sensor entity
        SELECT: Select entity
        LIGHT: Light entity
        BUTTON: Button entity
    """

    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    SELECT = "select"
    LIGHT = "light"
    BUTTON = "button"
    NUMBER = "number"
    SWITCH = "switch"


@dataclass
class EEPEntityDef:
    """Definition for a generic EEP-derived entity."""

    name: str
    rorg: int
    rorg_func: int
    rorg_type: int
    entity_type: EntityType = EntityType.SENSOR
    data_field: str | None = None
    unit: str | None = None
    device_class: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    enum_options: list[str] | None = None
    command: str | None = None
    offset: int | None = None
    size: int | None = None
