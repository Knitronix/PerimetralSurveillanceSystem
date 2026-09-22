"""
Controllo locale di una Shelly Plug (Plus Plug S / Plug S Gen3 / Gen2-Gen3 in generale)
via API RPC HTTP, senza cloud e senza account.

Requisiti:
    pip install requests

Uso:
    python shelly_control.py <ip> on
    python shelly_control.py <ip> off
    python shelly_control.py <ip> status
    python shelly_control.py <ip> toggle
"""

import sys
import requests

SHELLY_IP = "192.168.0.64"  # IP della presa Shelly
TIMEOUT = 5  # secondi


def _rpc(ip: str, method: str, params: dict | None = None) -> dict:
    """Chiama un metodo RPC sulla Shelly locale e ritorna il JSON di risposta."""
    url = f"http://{ip}/rpc/{method}"
    resp = requests.get(url, params=params or {}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def turn_on(ip: str, channel: int = 0) -> dict:
    return _rpc(ip, "Switch.Set", {"id": channel, "on": "true"})


def turn_off(ip: str, channel: int = 0) -> dict:
    return _rpc(ip, "Switch.Set", {"id": channel, "on": "false"})


def toggle(ip: str, channel: int = 0) -> dict:
    return _rpc(ip, "Switch.Toggle", {"id": channel})


def get_status(ip: str, channel: int = 0) -> dict:
    """Ritorna stato completo: output (on/off), potenza istantanea, ecc."""
    return _rpc(ip, "Switch.GetStatus", {"id": channel})


def is_on(ip: str, channel: int = 0) -> bool:
    return bool(get_status(ip, channel).get("output"))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    # Uso: python shelly_control.py on|off|status|toggle [ip]
    action = sys.argv[1].lower()
    ip_addr = sys.argv[2] if len(sys.argv) > 2 else SHELLY_IP

    if action == "on":
        result = turn_on(ip_addr)
    elif action == "off":
        result = turn_off(ip_addr)
    elif action == "toggle":
        result = toggle(ip_addr)
    elif action == "status":
        result = get_status(ip_addr)
    else:
        print(f"Azione sconosciuta: {action}")
        sys.exit(1)

    print(result)