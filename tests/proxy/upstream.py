"""A throwaway certificate authority and a local HTTPS server that stands in for a model API.

Nothing here reaches the public internet: the server listens on an ephemeral port on
127.0.0.1 and presents a certificate for `localhost` signed by a CA made per test.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True)
class Authority:
    ca_cert: Path
    server_cert: Path
    server_key: Path


def make_authority(directory: Path) -> Authority:
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "upstream test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    authority = Authority(directory / "ca.pem", directory / "server.pem", directory / "server.key")
    authority.ca_cert.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    authority.server_cert.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    authority.server_key.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return authority


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    body: bytes


@dataclass
class Answer:
    """What the model API answers every request with. `chunks` after the first are held back until `hold` is set,
    so a test can watch the first reach the client while the rest is still to come; sent chunked."""

    content_type: str
    chunks: list[bytes]
    hold: asyncio.Event | None = None


@dataclass
class Upstream:
    port: int = 0
    received: list[Received] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)


async def _answer(writer: asyncio.StreamWriter, answer: Answer) -> None:
    writer.write(
        f"HTTP/1.1 200 OK\r\ncontent-type: {answer.content_type}\r\ntransfer-encoding: chunked\r\n\r\n".encode()
    )
    for i, chunk in enumerate(answer.chunks):
        if i == 1 and answer.hold is not None:
            await answer.hold.wait()
        writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
        await writer.drain()
    writer.write(b"0\r\n\r\n")
    await writer.drain()


async def _serve(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, upstream: Upstream, answer: Answer | None
) -> None:
    try:
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            length = 0
            headers: dict[str, str] = {}
            for line in lines[1:]:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            body = await reader.readexactly(length) if length else b""
            upstream.received.append(Received(method, path, body))
            upstream.headers.append(headers)
            if answer is not None:
                await _answer(writer, answer)
                continue
            reply = json.dumps({"received": len(upstream.received)}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                + f"content-length: {len(reply)}\r\n\r\n".encode()
                + reply
            )
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
        pass
    finally:
        writer.close()


@asynccontextmanager
async def model_api(authority: Authority, answer: Answer | None = None) -> AsyncIterator[Upstream]:
    """An HTTPS server on 127.0.0.1 that records each request and answers 200: with `answer` when given, else
    with a count of the requests so far."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(authority.server_cert, authority.server_key)
    upstream = Upstream()
    open_writers: set[asyncio.StreamWriter] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        open_writers.add(writer)
        try:
            await _serve(reader, writer, upstream, answer)
        finally:
            open_writers.discard(writer)

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    upstream.port = server.sockets[0].getsockname()[1]
    try:
        yield upstream
    finally:
        server.close()
        for writer in list(open_writers):
            writer.close()
        await server.wait_closed()
