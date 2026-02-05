"""Support for EnOcean sensors."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import struct

import voluptuous as vol

from homeassistant.components.sensor import (
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
    ConfigEntry,
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_ID,
    CONF_NAME,
    CONF_UNIT_OF_MEASUREMENT,
    PERCENTAGE,
    STATE_CLOSED,
    STATE_OPEN,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DATA_ENOCEAN, LOGGER
from .entity import DynamicEnoceanEntity, EnOceanEntity, async_create_entities_from_eep
from .types import EEPEntityDef

CONF_MAX_TEMP = "max_temp"
CONF_MIN_TEMP = "min_temp"
CONF_RANGE_FROM = "range_from"
CONF_RANGE_TO = "range_to"
CONF_DATA_FIELD = "data_field"
CONF_AUTO_DISCOVER = "auto_discover"

DEFAULT_NAME = "EnOcean sensor"

SENSOR_TYPE_HUMIDITY = "humidity"
SENSOR_TYPE_POWER = "powersensor"
SENSOR_TYPE_TEMPERATURE = "temperature"
SENSOR_TYPE_WINDOWHANDLE = "windowhandle"


@dataclass(frozen=True, kw_only=True)
class EnOceanSensorEntityDescription(SensorEntityDescription):
    """Describes EnOcean sensor entity."""


SENSOR_DESC_TEMPERATURE = EnOceanSensorEntityDescription(
    key=SENSOR_TYPE_TEMPERATURE,
    name="Temperature",
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    device_class=SensorDeviceClass.TEMPERATURE,
    state_class=SensorStateClass.MEASUREMENT,
)

SENSOR_DESC_HUMIDITY = EnOceanSensorEntityDescription(
    key=SENSOR_TYPE_HUMIDITY,
    name="Humidity",
    native_unit_of_measurement=PERCENTAGE,
    device_class=SensorDeviceClass.HUMIDITY,
    state_class=SensorStateClass.MEASUREMENT,
)

SENSOR_DESC_POWER = EnOceanSensorEntityDescription(
    key=SENSOR_TYPE_POWER,
    name="Power",
    native_unit_of_measurement=UnitOfPower.WATT,
    device_class=SensorDeviceClass.POWER,
    state_class=SensorStateClass.MEASUREMENT,
)

SENSOR_DESC_WINDOWHANDLE = EnOceanSensorEntityDescription(
    key=SENSOR_TYPE_WINDOWHANDLE,
    name="WindowHandle",
    translation_key="window_handle",
)


PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_ID): vol.All(cv.ensure_list, [vol.Coerce(int)]),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DEVICE_CLASS, default=SENSOR_TYPE_POWER): cv.string,
        vol.Optional(CONF_MAX_TEMP, default=40): vol.Coerce(int),
        vol.Optional(CONF_MIN_TEMP, default=0): vol.Coerce(int),
        vol.Optional(CONF_RANGE_FROM, default=255): cv.positive_int,
        vol.Optional(CONF_RANGE_TO, default=0): cv.positive_int,
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
        vol.Optional(CONF_DATA_FIELD): cv.string,
        vol.Optional(CONF_AUTO_DISCOVER, default=False): cv.boolean,
    }
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up EnOcean sensor entities."""
    enocean_data = hass.data.get(DATA_ENOCEAN, {})

    # Register callback for EEP-discovered entities
    async def _add_entities_from_eep(
        device_id, entities_list, rorg, rorg_func, rorg_type
    ):
        """Add sensor entities for a discovered device from EEP profile."""

        await async_create_entities_from_eep(
            hass,
            config_entry,
            device_id,
            entities_list,
            rorg,
            rorg_func,
            rorg_type,
            platform_type="sensor",
            entity_class=DynamicEnOceanSensor,
            async_add_entities=async_add_entities,
        )

    # Register the callback in the platform callbacks registry
    platform_callbacks = enocean_data.get("platform_callbacks", {})
    platform_callbacks["sensor"] = _add_entities_from_eep


class EnOceanSensor(EnOceanEntity, RestoreSensor):
    """Representation of an EnOcean sensor device such as a power meter."""

    def __init__(
        self,
        dev_id: list[int],
        dev_name: str,
        description: EnOceanSensorEntityDescription,
    ) -> None:
        """Initialize the EnOcean sensor device."""
        # Convert UNDEFINED to None for attr_name
        attr_name_value: str | None = None
        if description.name:
            attr_name_value = str(description.name)
        super().__init__(
            dev_id,
            data_field=description.key,
            attr_name=attr_name_value,
            dev_name=dev_name,
        )

    async def async_added_to_hass(self) -> None:
        """Call when entity about to be added to hass."""
        # If not None, we got an initial value.
        await super().async_added_to_hass()
        if (sensor_data := await self.async_get_last_sensor_data()) is not None:
            self._attr_native_value = sensor_data.native_value


class EnOceanTemperatureSensor(EnOceanSensor):
    """Representation of an EnOcean temperature sensor device.

    EEPs (EnOcean Equipment Profiles):
    - A5-02-01 to A5-02-1B (Delta offset are not supported)
    - A5-04-01 (Temp. and Humidity Sensor, Range 0°C to +40°C and 0% to 100%)
    - A5-04-02 (Temp. and Humidity Sensor, Range -20°C to +60°C and 0% to 100%)
    - A5-10-10 (Temp. and Humidity Sensor and Set Point)
    - A5-10-12 (Temp. and Humidity Sensor, Set Point and Occupancy Control)
    - 10 Bit Temp. Sensors are not supported (A5-02-20, A5-02-30)

    For the following EEPs the scales must be set to "0 to 250":
    - A5-04-01
    - A5-04-02
    - A5-10-10 to A5-10-14
    """

    def __init__(
        self,
        dev_id: list[int],
        dev_name: str,
        description: EnOceanSensorEntityDescription,
        *,
        scale_min: int,
        scale_max: int,
        range_from: int,
        range_to: int,
    ) -> None:
        """Initialize the EnOcean temperature sensor device."""
        super().__init__(dev_id, dev_name, description)
        self._scale_min = scale_min
        self._scale_max = scale_max
        self.range_from = range_from
        self.range_to = range_to

    @callback
    def value_changed(self, packet):
        """Update the internal state of the sensor."""
        if packet.data[0] != 0xA5:
            return
        temp_scale = self._scale_max - self._scale_min
        temp_range = self.range_to - self.range_from
        raw_val = packet.data[3]
        temperature = temp_scale / temp_range * (raw_val - self.range_from)
        temperature += self._scale_min
        self._attr_native_value = round(temperature, 1)
        self.schedule_update_ha_state()


class EnOceanHumiditySensor(EnOceanSensor):
    """Representation of an EnOcean humidity sensor device.

    EEPs (EnOcean Equipment Profiles):
    - A5-04-01 (Temp. and Humidity Sensor, Range 0°C to +40°C and 0% to 100%)
    - A5-04-02 (Temp. and Humidity Sensor, Range -20°C to +60°C and 0% to 100%)
    - A5-10-10 to A5-10-14 (Room Operating Panels)
    """

    @callback
    def value_changed(self, packet):
        """Update the internal state of the sensor."""
        if packet.rorg != 0xA5:
            return
        humidity = packet.data[2] * 100 / 250
        self._attr_native_value = round(humidity, 1)
        self.schedule_update_ha_state()


class EnOceanWindowHandle(EnOceanSensor):
    """Representation of an EnOcean window handle device.

    EEPs (EnOcean Equipment Profiles):
    - F6-10-00 (Mechanical handle / Hoppe AG)
    """

    @callback
    def value_changed(self, packet):
        """Update the internal state of the sensor."""
        action = (packet.data[1] & 0x70) >> 4

        if action == 0x07:
            self._attr_native_value = STATE_CLOSED
        if action in (0x04, 0x06):
            self._attr_native_value = STATE_OPEN
        if action == 0x05:
            self._attr_native_value = "tilt"

        self.schedule_update_ha_state()


class DynamicEnOceanSensor(DynamicEnoceanEntity, EnOceanSensor):
    """Generic dynamic sensor that parses EEP profiles using the generic Parser.

    This sensor can be configured per-instance with an explicit EEP profile
    (rorg/func/type) and an optional fields mapping. If no per-instance
    profile is provided it will fall back to the preloaded Ventilairsec
    parser/fields when available.
    """

    def __init__(
        self,
        dev_id: list[int],
        dev_name: str,
        rorg: int,
        rorg_func: int,
        rorg_type: int,
        data_field: str,
        device_class: SensorDeviceClass | str | None = None,
        fields: EEPEntityDef | None = None,
        attr_name: str | None = None,
    ) -> None:
        """Initialize the dynamic EnOcean sensor.

        Args:
            dev_id: List of device ID bytes
            dev_id_hex: Hex string representation of device ID
            dev_name: Human-readable device name
            data_field: EEP field name to extract
            device_class: Device class for the sensor
            rorg/rorg_func/rorg_type: EEP identifiers for per-instance parsing
            fields: Optional preloaded fields mapping (from load_eep_fields)
            attr_name: Optional entity attribute name (defaults to data_field)
            description: Optional entity description
        """
        EnOceanSensor.__init__(
            self,
            dev_id=dev_id,
            dev_name=dev_name,
            description=EnOceanSensorEntityDescription(
                key=data_field or "sensor",
                name=attr_name or data_field or dev_name,
            ),
        )

        # Initialize shared dynamic behaviour
        DynamicEnoceanEntity.__init__(
            self,
            dev_id,
            data_field=data_field or "sensor",
            rorg=rorg,
            rorg_func=rorg_func,
            rorg_type=rorg_type,
            dev_name=dev_name,
            fields=fields,
        )
        # Set sensor-specific attributes
        self._attr_name = attr_name or data_field or dev_name

        # Apply all EEP field properties if available
        if fields is not None and isinstance(fields, EEPEntityDef):
            if fields.unit:
                self._unit = fields.unit
                self._attr_native_unit_of_measurement = fields.unit

            if fields.device_class:
                contextlib.suppress(ValueError)
                with contextlib.suppress(ValueError):
                    self._attr_device_class = SensorDeviceClass(fields.device_class)
            if fields.state_class:
                contextlib.suppress(ValueError)
                with contextlib.suppress(ValueError):
                    self._attr_state_class = SensorStateClass(fields.state_class)
        # Override with explicit device_class parameter if provided
        if device_class is not None:
            # Normalize device_class to enum if it's a string
            if isinstance(device_class, str):
                contextlib.suppress(ValueError)
                with contextlib.suppress(ValueError):
                    self._attr_device_class = SensorDeviceClass(device_class)
            else:
                self._attr_device_class = device_class
            # Ensure a valid native unit is set for temperature device class
            if (device_class == SensorDeviceClass.TEMPERATURE) or (
                isinstance(device_class, str) and device_class.lower() == "temperature"
            ):
                # If no unit provided via fields, default to Celsius
                if not getattr(self, "_attr_native_unit_of_measurement", None):
                    self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def value_changed(self, packet):
        """Update the internal state of the sensor when a packet arrives."""
        if not packet.data or len(packet.data) < 2:
            return

        try:
            # Packet should already be parsed by dongle callback
            if not packet.parsed or not self._data_field:
                return

            if self._fields:
                # `self._fields` is expected to be an `EEPEntityDef` dataclass.
                # Extract the raw parsed value and apply any enum mapping
                # provided by the dataclass instead of delegating to the
                # library function which expects a dict-like metadata object.
                raw = self._get_parsed_value(packet, self._data_field)
                if raw is None:
                    value = None
                else:
                    enum_opts = getattr(self._fields, "enum_options", None)
                    if enum_opts and isinstance(enum_opts, (list, tuple)):
                        try:
                            idx = int(raw)
                            value = enum_opts[idx] if 0 <= idx < len(enum_opts) else raw
                        except (ValueError, TypeError):
                            value = raw
                    else:
                        value = raw
            else:
                value = self._get_parsed_value(packet, self._data_field)

            if value is not None:
                self._attr_native_value = value
                self.schedule_update_ha_state()
        except (ValueError, KeyError, OSError, TypeError, struct.error) as err:
            LOGGER.error(
                "Error processing dynamic sensor packet for %s: %s",
                self._attr_name,
                err,
            )
