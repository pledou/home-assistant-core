"""Support for EnOcean devices."""

from collections.abc import Awaitable, Callable
import logging

from enocean.protocol.eep import get_eep as _get_eep
import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_DEVICE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType

from .const import DATA_ENOCEAN, DOMAIN, ENOCEAN_DONGLE, PLATFORMS
from .dongle import SIGNAL_DISCOVER_DEVICE, EnOceanDongle
from .eep_devices import load_device_profile_from_packet
from .entity import format_device_id_hex, format_device_id_hex_underscore
from .types import DiscoveryInfo, EEPEntityDef, EepProfile

# Registry for platform entity add callbacks
PLATFORM_ADD_ENTITIES_CALLBACKS: dict[
    str, Callable[[list[int], list[EEPEntityDef], int, int, int], Awaitable[None]]
] = {}

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({vol.Required(CONF_DEVICE): cv.string})},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the EnOcean component."""
    # support for text-based configuration (legacy)
    if DOMAIN not in config:
        return True

    enocean_config = config[DOMAIN]

    if hass.config_entries.async_entries(DOMAIN):
        # We can only have one dongle. If there is already one in the config,
        # there is no need to import the yaml based config.
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=enocean_config
        )
    )

    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up the EnOcean dongle (following ZHA pattern)."""
    enocean_data = hass.data.setdefault(DATA_ENOCEAN, {})
    enocean_data["platform_callbacks"] = {}

    # Only the dongle config entry is supported
    if CONF_DEVICE not in config_entry.data:
        _LOGGER.warning("Config entry has no device path, skipping setup")
        return False

    usb_dongle = EnOceanDongle(hass, config_entry.data[CONF_DEVICE])
    await usb_dongle.async_setup()
    enocean_data[ENOCEAN_DONGLE] = usb_dongle

    # Set up platforms using modern config entry approach
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Set up device discovery listener (ZHA-style: add to device registry)
    async def async_device_discovered(discovery_info: DiscoveryInfo) -> None:
        """Handle device discovery signal from dongle - add device to registry.

        This async function processes device discovery and runs blocking EEP
        file operations in the executor rather than inside the event loop.
        """
        device_id = discovery_info["device_id"]
        # Convert device_id from list[int] to hex:hex:hex:hex format for device registry
        device_id_hex = format_device_id_hex(device_id)

        # Narrow the TypedDict to a local variable so types are preserved
        eep_profile: EepProfile = discovery_info["eep_profile"]
        rorg = eep_profile["rorg"]
        rorg_func = eep_profile["rorg_func"]
        rorg_type = eep_profile["rorg_type"]

        device_registry = dr.async_get(hass)
        # Check if device already exists before creating
        existing_device = device_registry.async_get_device(
            identifiers={(DOMAIN, device_id_hex)}
        )

        if existing_device is None:
            # If python-enocean's EEP parser is available, check that the
            # (RORG, FUNC, TYPE) tuple exists in the EEP database. The
            # EEP.find_profile method will normalize multi-byte RORGs.
            # If the python-enocean EEP parser is available, try to resolve the
            # (RORG, FUNC, TYPE) tuple to a known profile. If not available or
            # lookup fails, fall back to the simpler func presence check below.
            # Defer any potentially blocking EEP parsing to the executor
            # to avoid opening files in the event loop.
            profile = None

            if _get_eep is not None:
                try:
                    # Get cached EEP parser instance in executor (it may open files)
                    _eep = await hass.async_add_executor_job(_get_eep)

                    # Try to resolve the full <profile> element from the parser's
                    # internal telegrams mapping. Calling into the mapping is
                    # potentially file/CPU-bound, so run in the executor.
                    def _get_full_profile(
                        eep, rorg: int, rorg_func: int, ptype: int
                    ) -> dict | None:
                        try:
                            return eep.telegrams[rorg][rorg_func][ptype]
                        except (
                            FileNotFoundError,
                            OSError,
                            ValueError,
                            KeyError,
                        ) as err:
                            _LOGGER.warning(
                                "Error getting eep profile for device %s rorg=0x%02x func=0x%02x type=0x%02x: %s",
                                eep,
                                rorg,
                                rorg_func,
                                ptype,
                                err,
                            )
                            return None

                    profile = await hass.async_add_executor_job(
                        _get_full_profile, _eep, rorg, rorg_func, rorg_type
                    )

                    # Fall back to the library's find_profile if direct lookup
                    # did not return a full <profile> element (older library
                    # behaviour may require it).
                    if profile is None:
                        profile = await hass.async_add_executor_job(
                            _eep.find_profile, None, rorg, rorg_func, rorg_type
                        )
                except (ValueError, TypeError, LookupError) as err:
                    _LOGGER.debug("EEP profile lookup failed: %s", err)
                    profile = None

            if profile is None:
                # If the EEP parser is not available, require func to be present
                # before accepting the device; otherwise log a warning and skip.
                if _eep is None:
                    _LOGGER.error(
                        "Unable to verify EEP profile for device %s as python-enocean EEP parser is not installed; "
                    )
                else:
                    _LOGGER.warning(
                        "Device %s has unknown EEP profile (rorg=0x%02x func=0x%02x type=0x%02x), skipping integration",
                        format_device_id_hex(device_id),
                        rorg,
                        rorg_func,
                        rorg_type,
                    )
                    return

            _LOGGER.info(
                "Discovered EnOcean device: %s rorg=0x%02x, func=0x%02x type=0x%02x",
                format_device_id_hex(device_id),
                rorg,
                rorg_func,
                rorg_type,
            )

            # Load and populate entities from the resolved EEP profile object
            eep_profile_data = await hass.async_add_executor_job(
                load_device_profile_from_packet,
                {
                    "rorg": rorg,
                    "rorg_func": rorg_func,
                    "rorg_type": rorg_type,
                    "eep_profile": profile,
                },
            )

            if eep_profile_data:
                entities = eep_profile_data

                # Register device EEP profile with dongle for systematic packet parsing
                usb_dongle.register_device_profile(
                    device_id, rorg, rorg_func, rorg_type
                )

                device_registry.async_get_or_create(
                    config_entry_id=config_entry.entry_id,
                    identifiers={(DOMAIN, format_device_id_hex_underscore(device_id))},
                    name=f"{DOMAIN} {format_device_id_hex(device_id)}",
                    manufacturer="EnOcean",
                    model=f"0x{rorg:02x} (func=0x{(rorg_func or 0):02x}, type=0x{rorg_type:02x})",
                )

                def _ent_val(ent, key):
                    if isinstance(ent, dict):
                        return ent.get(key)
                    return getattr(ent, key, None)

                _LOGGER.debug(
                    "Entity details: %s",
                    [
                        {
                            "description": _ent_val(e, "description"),
                            "data_field": _ent_val(e, "data_field"),
                            "entity_type": _ent_val(e, "entity_type"),
                        }
                        for e in entities[:5]
                    ],
                )
                # Call registered platform callbacks directly to add entities
                platform_callbacks = enocean_data.get("platform_callbacks", {})
                for platform_name, callback in platform_callbacks.items():
                    try:
                        await callback(
                            device_id,
                            entities,
                            rorg,
                            rorg_func,
                            rorg_type,
                        )
                    except (
                        TimeoutError,
                        RuntimeError,
                        ValueError,
                        TypeError,
                        LookupError,
                        OSError,
                    ):
                        _LOGGER.exception(
                            "Error calling platform callback %s for device %s",
                            platform_name,
                            format_device_id_hex(device_id),
                        )
            else:
                # Signal platforms without entities if profile couldn't be loaded
                _LOGGER.warning(
                    "No entities created for device %s as EEP profile %02x-%02x-%02x could not be loaded, device has not been integrated",
                    format_device_id_hex(device_id),
                    rorg,
                    rorg_func,
                    rorg_type,
                )

    # Register listener for device discovery signals
    async def _handle_device_discovered(discovery_info: DiscoveryInfo) -> None:
        """Handle device discovery signal from dispatcher."""
        await async_device_discovered(discovery_info)

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, SIGNAL_DISCOVER_DEVICE, _handle_device_discovered
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload EnOcean config entry."""

    enocean_data = hass.data.get(DATA_ENOCEAN, {})

    enocean_dongle = enocean_data.get(ENOCEAN_DONGLE)
    if enocean_dongle:
        enocean_dongle.unload()

    sensor_manager = enocean_data.get("sensor_manager")
    if sensor_manager:
        await sensor_manager.async_unload()

    hass.data.pop(DATA_ENOCEAN, None)

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove a device from the EnOcean integration.

    This allows users to delete discovered EnOcean devices from the UI.
    The device will be re-discovered if it sends another packet.
    """
    # Check if device belongs to this config entry
    if config_entry.entry_id not in device_entry.config_entries:
        return False

    # Check if device is an EnOcean device (has correct identifier format)
    for identifier in device_entry.identifiers:
        if identifier[0] == DOMAIN:
            _LOGGER.info(
                "Removing EnOcean device %s (%s) from integration",
                device_entry.name,
                identifier[1],
            )
            # Allow removal - device will be re-discovered if it sends packets
            return True

    return False
