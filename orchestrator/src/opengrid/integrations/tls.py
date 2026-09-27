"""Client TLS settings shared by every integration adapter (one owner for certificate loading).

Certificates and keys are FILES on the host (`/etc/opengrid/certs/...`, mode 640, never in git). A
private-key passphrase, when there is one, is a secret and is referenced by env-var NAME only
(`key_password_env`), resolved through `opengrid.platform.config.resolve_secret`.
"""

from __future__ import annotations

import ssl
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from opengrid.platform.config import resolve_secret

__all__ = [
    "ServerTlsSettings",
    "TlsSettings",
    "build_client_ssl_context",
    "build_server_ssl_context",
    "peer_common_name",
]


class TlsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    ca_file: str | None = None  # trust anchor for the server certificate (the utility / ISO CA)
    cert_file: str | None = None  # our client certificate (PEM)
    key_file: str | None = None  # our private key (PEM)
    key_password_env: str | None = None
    server_hostname: str | None = None  # override SNI / hostname check (e.g. connecting by IP)
    ciphers: str | None = None  # e.g. "ECDHE-ECDSA-AES128-CCM8" for IEEE 2030.5
    minimum_version: str = "TLSv1_2"


def build_client_ssl_context(settings: TlsSettings) -> ssl.SSLContext | None:
    """An `SSLContext` for a mutually authenticated client connection, or `None` when TLS is off.
    Hostname checking and certificate verification are always on when TLS is on."""
    if not settings.enabled:
        return None
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=settings.ca_file)
    context.minimum_version = ssl.TLSVersion[settings.minimum_version]
    if settings.ciphers:
        context.set_ciphers(settings.ciphers)
    if settings.cert_file:
        for path in (settings.cert_file, settings.key_file):
            if path is not None and not Path(path).is_file():
                raise FileNotFoundError(f"TLS file not found: {path}")
        password = resolve_secret(settings.key_password_env) if settings.key_password_env else None
        context.load_cert_chain(settings.cert_file, settings.key_file, password=password)
    return context


class ServerTlsSettings(BaseModel):
    """TLS for an adapter that LISTENS (the grid link's outstation). Always mutual when enabled: the
    peer must present a certificate issued by `client_ca_file` (IEC 62351-3 profile)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    cert_file: str | None = None  # our server certificate (PEM)
    key_file: str | None = None
    key_password_env: str | None = None
    client_ca_file: str | None = None  # trust anchor for the utility's client certificates
    ciphers: str | None = None
    minimum_version: str = "TLSv1_2"


def build_server_ssl_context(settings: ServerTlsSettings) -> ssl.SSLContext | None:
    """A server `SSLContext` that REQUIRES a client certificate, or `None` when TLS is off. Raises
    `FileNotFoundError` / `ValueError` on missing files or settings: a listener never silently falls
    back to plaintext."""
    if not settings.enabled:
        return None
    if not (settings.cert_file and settings.key_file and settings.client_ca_file):
        raise ValueError("server TLS needs cert_file, key_file and client_ca_file")
    for path in (settings.cert_file, settings.key_file, settings.client_ca_file):
        if not Path(path).is_file():
            raise FileNotFoundError(f"TLS file not found: {path}")
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=settings.client_ca_file)
    context.minimum_version = ssl.TLSVersion[settings.minimum_version]
    context.verify_mode = ssl.CERT_REQUIRED
    if settings.ciphers:
        context.set_ciphers(settings.ciphers)
    password = resolve_secret(settings.key_password_env) if settings.key_password_env else None
    context.load_cert_chain(settings.cert_file, settings.key_file, password=password)
    return context


def peer_common_name(peercert: object) -> str | None:
    """The subject commonName of a verified peer certificate (`SSLSocket.getpeercert()` shape), or None."""
    if not isinstance(peercert, dict):
        return None
    for rdn in peercert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return str(value)
    return None
