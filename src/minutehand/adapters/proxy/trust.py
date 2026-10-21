"""The certificates an agent must trust: the proxy's own CA, and every public root besides.

An agent's HTTP library reads ONE file of trusted CAs from `SSL_CERT_FILE` (or its own variable), and that
file replaces the roots it would otherwise trust. A file holding only the proxy's CA verifies the hosts the
proxy answers, and breaks every call it tunnels to a real host, such as a model API, whose certificate is
signed by a public CA. So the file handed to the agent is a bundle: the public roots `certifi` carries, then
the proxy's CA.

The CA is made in the state directory the first time it is needed, by the proxy or by `minutehand env`, and
reused after, so an agent configured once keeps trusting every later run.

The agent's clock is the run's, not the machine's (`docs/design.md`, "A sandbox whose clock Minutehand owns"), so
the day it verifies a certificate on is the scenario's: any year. mitmproxy dates a CA and every host certificate it
signs from the machine's clock (valid from two days before it), and an agent whose run began earlier than that
refuses them all as not yet valid. So the CA and every certificate the proxy presents are valid at every instant a
clock can read, from the epoch on, with no end; the machine's clock never names a date in them.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from mitmproxy import certs
from mitmproxy.options import CONF_BASENAME

CA_CERT = f"{CONF_BASENAME}-ca-cert.pem"
CA_KEY = f"{CONF_BASENAME}-ca.pem"
"""The CA's key and certificate in one file, the one mitmproxy loads; while it is missing there is no CA."""
BUNDLE = "minutehand-ca-bundle.pem"
KEY_SIZE = 2048
"""mitmproxy's own default, so a CA made here is the one the proxy would have made."""
VALID_FROM = datetime(1970, 1, 1)
VALID_UNTIL = datetime(9999, 12, 31, 23, 59, 59)
"""Every instant a clock can read: a certificate is checked against the agent's clock, which is the run's."""


def authority(confdir: Path) -> Path:
    """The proxy's CA certificate in `confdir`, made when it is not there yet.

    Made when mitmproxy's `CertStore.from_store` would make it (its key file is missing), in the files it loads,
    valid at every instant. One already there is not loaded here: parsing and checking its RSA key is most of a
    proxy's start, and the proxy loads it itself."""
    confdir.mkdir(parents=True, exist_ok=True)
    if not (confdir / CA_KEY).exists():
        _make_ca(confdir)
    return confdir / CA_CERT


def _make_ca(confdir: Path) -> None:
    """mitmproxy's `create_ca` and `create_store`, but for the dates: the CA key and certificate in one PEM, and the
    certificate alone beside it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, CONF_BASENAME),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, CONF_BASENAME),
        ]
    )
    ca = (
        x509.CertificateBuilder()
        .serial_number(x509.random_serial_number())
        .subject_name(name)
        .not_valid_before(VALID_FROM)
        .not_valid_after(VALID_UNTIL)
        .issuer_name(name)
        .public_key(key.public_key())
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    pem_cert = ca.public_bytes(serialization.Encoding.PEM)
    with certs.CertStore.umask_secret():
        (confdir / CA_KEY).write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
            + pem_cert
        )
    (confdir / CA_CERT).write_bytes(pem_cert)


class EverValidCertStore(certs.CertStore):
    """mitmproxy's certificate store, whose host certificates are valid at every instant (`VALID_FROM`, `VALID_UNTIL`)
    instead of from two days before the machine's clock; the store the proxy is given in place of its own."""

    @classmethod
    def open(cls, confdir: Path) -> EverValidCertStore:
        authority(confdir)
        store = cls.from_store(path=confdir, basename=CONF_BASENAME, key_size=KEY_SIZE)
        assert isinstance(store, cls)  # `from_store` builds `cls`, and says it builds its base
        return store

    def get_cert(
        self,
        commonname: str | None,
        sans: Iterable[x509.GeneralName],
        organization: str | None = None,
        crl_url: str | None = None,
    ) -> certs.CertStoreEntry:
        """As mitmproxy's: one certificate per host (or wildcard) asked for, made on first use."""
        sans = certs._fix_legacy_sans(sans)  # pyright: ignore[reportPrivateUsage]
        potential_keys: list[certs.TCertId] = []
        if commonname:
            potential_keys.extend(self.asterisk_forms(commonname))
        for san in sans:
            potential_keys.extend(self.asterisk_forms(san))
        potential_keys.append("*")
        potential_keys.append((commonname, sans))
        name = next(filter(lambda key: key in self.certs, potential_keys), None)
        if name:
            return self.certs[name]
        entry = certs.CertStoreEntry(
            cert=_host_cert(self.default_privatekey, self.default_ca._cert, commonname, sans, organization, crl_url),  # pyright: ignore[reportPrivateUsage]
            privatekey=self.default_privatekey,
            chain_file=self.default_chain_file,
            chain_certs=self.default_chain_certs,
        )
        self.certs[(commonname, sans)] = entry
        self.expire(entry)
        return entry


def _host_cert(
    privkey: rsa.RSAPrivateKey,
    cacert: x509.Certificate,
    commonname: str | None,
    sans: x509.GeneralNames,
    organization: str | None,
    crl_url: str | None,
) -> certs.Cert:
    """mitmproxy's `dummy_cert`, but for the dates."""
    builder = (
        x509.CertificateBuilder()
        .issuer_name(cacert.subject)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .public_key(cacert.public_key())
        .not_valid_before(VALID_FROM)
        .not_valid_after(VALID_UNTIL)
    )
    subject: list[x509.NameAttribute] = []
    is_valid_commonname = commonname is not None and len(commonname) < 64
    if commonname is not None and is_valid_commonname:
        subject.append(x509.NameAttribute(NameOID.COMMON_NAME, commonname))
    if organization is not None:
        subject.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization))
    builder = builder.subject_name(x509.Name(subject)).serial_number(x509.random_serial_number())
    # RFC 5280 §4.2.1.6: subjectAltName is critical if subject is empty.
    builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=not is_valid_commonname)
    # RFC 5280 §4.2.1.2: the AKI is the issuer's SKI, byte for byte, when the issuer has one.
    try:
        issuer_ski = cacert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
        aki = x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(issuer_ski)
    except x509.ExtensionNotFound:
        aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(cacert.public_key())  # pyright: ignore[reportArgumentType]
    builder = builder.add_extension(aki, critical=False)
    if crl_url:
        builder = builder.add_extension(
            x509.CRLDistributionPoints(
                [
                    x509.DistributionPoint(
                        [x509.UniformResourceIdentifier(crl_url)], relative_name=None, crl_issuer=None, reasons=None
                    )
                ]
            ),
            critical=False,
        )
    return certs.Cert(builder.sign(private_key=privkey, algorithm=hashes.SHA256()))


def write_bundle(confdir: Path, *, public_roots: str | None = None) -> Path:
    """The one file every CA variable names: `public_roots` (certifi's by default), then the proxy's CA.

    Rewritten on every call, so a newer certifi or a remade CA reaches the next run; written beside and moved into
    place, so an agent of another run on the same state directory reading it meanwhile reads it whole."""
    roots = certifi.contents() if public_roots is None else public_roots
    ours = authority(confdir).read_text(encoding="utf-8")
    bundle = confdir / BUNDLE
    with tempfile.NamedTemporaryFile("w", dir=confdir, prefix=f".{BUNDLE}.", delete=False, encoding="utf-8") as kept:
        kept.write(roots.rstrip("\n") + "\n\n# Minutehand's proxy CA\n" + ours)
    os.replace(kept.name, bundle)
    return bundle
