"""Loaded by a client process the Drive tests start, before anything else: it refuses every connection that is
not to this machine. A client that ignored the proxy it was handed would otherwise reach Google itself; with
this it fails, loudly, naming the host it tried."""

import socket

_LOCAL = ("127.0.0.1", "::1", "localhost")
_connect = socket.socket.connect
_create_connection = socket.create_connection


def _refuse(address: object) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if host not in _LOCAL:
        raise OSError(f"offline: a direct connection to {address} was refused; only the proxy may be reached")


def _guarded_connect(self: socket.socket, address: object) -> None:
    _refuse(address)
    _connect(self, address)  # type: ignore[arg-type]


def _guarded_create_connection(address: tuple[str, int], *args: object, **kwargs: object) -> socket.socket:
    _refuse(address)
    return _create_connection(address, *args, **kwargs)  # type: ignore[arg-type]


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
