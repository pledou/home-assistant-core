"""Test out-of-range value validation for EnOcean entities."""

from unittest.mock import MagicMock

import pytest

from homeassistant.components.enocean.entity import EnOceanEntity
from homeassistant.core import HomeAssistant


@pytest.fixture
def mock_packet_valid():
    """Create a mock packet with valid data."""
    packet = MagicMock()
    packet.sender = [0x04, 0x20, 0x58, 0xA5]
    packet.destination = [0xFF, 0x9C, 0x80, 0x80]
    packet.dBm = -77
    packet.packet_type = 0x01
    packet.data = [0xD1, 0x07, 0x90, 0x01, 0x02, 0x00, 0x04, 0x20, 0x1C, 0x1C, 0x00]
    packet.optional = [0x01, 0xFF, 0x9C, 0x80, 0x80, 0x4D, 0x00]
    packet.parsed = {
        "CMD": {"value": 0, "raw_value": 0, "out_of_range": False},
        "TEMPCELEC": {"value": 4.0, "raw_value": 4, "out_of_range": False},
        "TEMPMSOUFFL": {"value": 32.0, "raw_value": 32, "out_of_range": False},
        "TEMPCHYDROR": {"value": 28.0, "raw_value": 28, "out_of_range": False},
    }
    return packet


@pytest.fixture
def mock_packet_invalid():
    """Create a mock packet with out-of-range data."""
    packet = MagicMock()
    packet.sender = [0x04, 0x20, 0x58, 0xA5]
    packet.destination = [0x04, 0x20, 0x74, 0xC9]
    packet.dBm = -89
    packet.packet_type = 0x01
    packet.data = [0xD1, 0x07, 0x90, 0x04, 0x00, 0x82, 0x2F, 0x04, 0x20, 0x58, 0xA5]
    packet.optional = [0x01, 0x04, 0x20, 0x74, 0xC9, 0x59, 0x00]
    packet.parsed = {
        "CMD": {"value": 4, "raw_value": 4, "out_of_range": False},
        "TEMPCELEC": {
            "value": 47.0,
            "raw_value": 47,
            "out_of_range": True,
        },  # Out of range [0-18]
        "TEMPMSOUFFL": {
            "value": 4.0,
            "raw_value": 4,
            "out_of_range": True,
        },  # Out of range [20-45]
        "TEMPCHYDROR": {
            "value": 32.0,
            "raw_value": 32,
            "out_of_range": True,
        },  # Out of range [8-28]
    }
    return packet


async def test_valid_packet_accepted(
    hass: HomeAssistant, mock_packet_valid, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that valid packets are accepted and processed."""
    entity = EnOceanEntity(
        dev_id=[0x04, 0x20, 0x58, 0xA5],
        data_field="test_field",
        attr_name="Test Entity",
    )
    entity.hass = hass

    # Mock value_changed to track if it was called
    value_changed_called = False

    def mock_value_changed(packet):
        nonlocal value_changed_called
        value_changed_called = True

    entity.value_changed = mock_value_changed

    # Process the packet
    entity._message_received_callback(mock_packet_valid)

    # Should process the packet
    assert value_changed_called
    assert "out-of-range" not in caplog.text.lower()


async def test_invalid_packet_rejected(
    hass: HomeAssistant, mock_packet_invalid, caplog: pytest.LogCaptureFixture
) -> None:
    """Test that packets with out-of-range values are rejected and logged."""
    entity = EnOceanEntity(
        dev_id=[0x04, 0x20, 0x58, 0xA5],
        data_field="test_field",
        attr_name="Test Entity",
    )
    entity.hass = hass

    # Mock value_changed to track if it was called
    value_changed_called = False

    def mock_value_changed(packet):
        nonlocal value_changed_called
        value_changed_called = True

    entity.value_changed = mock_value_changed

    # Process the packet
    entity._message_received_callback(mock_packet_invalid)

    # Should NOT process the packet
    assert not value_changed_called

    # Should log a warning
    assert "out-of-range" in caplog.text.lower()
    assert "04:20:58:a5" in caplog.text.lower()


async def test_out_of_range_detection(hass: HomeAssistant, mock_packet_invalid) -> None:
    """Test that _has_out_of_range_fields correctly detects invalid fields."""
    entity = EnOceanEntity(
        dev_id=[0x04, 0x20, 0x58, 0xA5],
        data_field="test_field",
    )
    entity.hass = hass

    # Should detect out-of-range fields
    assert entity._has_out_of_range_fields(mock_packet_invalid) is True


async def test_no_out_of_range_detection(
    hass: HomeAssistant, mock_packet_valid
) -> None:
    """Test that _has_out_of_range_fields returns False for valid packets."""
    entity = EnOceanEntity(
        dev_id=[0x04, 0x20, 0x58, 0xA5],
        data_field="test_field",
    )
    entity.hass = hass

    # Should NOT detect out-of-range fields
    assert entity._has_out_of_range_fields(mock_packet_valid) is False
