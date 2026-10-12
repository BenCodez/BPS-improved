"""Bermuda's retained entity identity and actual Bluetooth observation address."""
from collections.abc import Mapping

from .bermuda_diagnostics import MAC


def _field(device, key):
    return device.get(key) if isinstance(device, Mapping) else getattr(device, key, None)


def _mac(value):
    return value.lower() if isinstance(value, str) and MAC.fullmatch(value) else None


def retained_mac(device):
    # Matches Bermuda range entity unique IDs, rather than guessing the BLE
    # address's relationship to its Wi-Fi MAC. Invalid declared proof is blocked.
    return _mac(_field(device, "address_wifi_mac") or _field(device, "address"))


def ble_mac(device):
    return _mac(_field(device, "address"))
