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

__all__ = ["TlsSettings", "build_client_ssl_context"]


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
