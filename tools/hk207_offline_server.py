"""Run the normal application locally with external socket requests forbidden.

This acceptance harness changes no routes, collection policies, or saved data.
Use a migrated scratch database and seed its local stock directory before
starting, so the unrelated A-share directory bootstrap does not need a provider.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn


def forbid_external_connections(log_path: Path) -> None:
    """Allow local app/browser traffic, rejecting and recording upstream attempts."""
    original_connect = socket.socket.connect
    original_lookup = socket.getaddrinfo

    def allowed(host: object) -> bool:
        if host is None or host == "localhost":
            return True
        try:
            return ipaddress.ip_address(str(host)).is_loopback
        except ValueError:
            return False

    def refuse(host: object) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": datetime.now(UTC).isoformat(), "host": str(host)}) + "\n")
        raise OSError(f"HK2-07 offline acceptance forbids an external connection to {host}")

    def connect(sock: socket.socket, address: Any) -> Any:
        if sock.family != socket.AF_UNIX and isinstance(address, tuple) and not allowed(address[0]):
            refuse(address[0])
        return original_connect(sock, address)

    def lookup(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not allowed(host):
            refuse(host)
        return original_lookup(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.getaddrinfo = lookup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--denied-log", type=Path, required=True)
    args = parser.parse_args()
    forbid_external_connections(args.denied_log)
    uvicorn.run("investment_agent.api.app:create_app", factory=True, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
