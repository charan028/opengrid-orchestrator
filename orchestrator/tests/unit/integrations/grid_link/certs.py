"""Throw-away test PKI for the grid-link TLS tests: a CA, a server certificate for 127.0.0.1 and client
certificates with chosen CNs, written under the test's own temporary directory (never a system path)."""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_CA_USAGE = x509.KeyUsage(
    digital_signature=True,
    content_commitment=False,
    key_encipherment=False,
    data_encipherment=False,
    key_agreement=False,
    key_cert_sign=True,
    crl_sign=True,
    encipher_only=False,
    decipher_only=False,
)


@dataclass(frozen=True)
class Pem:
    cert: str
    key: str


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write(directory: Path, stem: str, cert: x509.Certificate, key: ec.EllipticCurvePrivateKey) -> Pem:
    cert_path, key_path = directory / f"{stem}.pem", directory / f"{stem}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return Pem(str(cert_path), str(key_path))


class Pki:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._ca_key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.UTC)
        self._ca = (
            x509.CertificateBuilder()
            .subject_name(_name("og-test-ca"))
            .issuer_name(_name("og-test-ca"))
            .public_key(self._ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self._ca_key.public_key()), critical=False
            )
            .add_extension(_CA_USAGE, critical=True)
            .sign(self._ca_key, hashes.SHA256())
        )
        self.ca = _write(directory, "ca", self._ca, self._ca_key)

    def issue(self, cn: str, *, server: bool = False) -> Pem:
        key = ec.generate_private_key(ec.SECP256R1())
        now = dt.datetime.now(dt.UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(_name(cn))
            .issuer_name(self._ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_key.public_key()), critical=False
            )
            .add_extension(
                x509.ExtendedKeyUsage(
                    [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
                ),
                critical=False,
            )
        )
        if server:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False,
            )
        return _write(self.directory, cn, builder.sign(self._ca_key, hashes.SHA256()), key)
