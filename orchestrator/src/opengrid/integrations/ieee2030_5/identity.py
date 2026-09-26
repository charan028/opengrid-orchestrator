"""IEEE 2030.5 device identity from the client certificate (2030.5-2018 S6.3.4).

LFDI = the leftmost 160 bits of SHA-256 over the DER-encoded device certificate (40 hex digits);
SFDI = the leftmost 36 bits of that hash as a decimal number, followed by one check digit that makes
the sum of all digits a multiple of 10.
"""

from __future__ import annotations

import hashlib

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

__all__ = ["lfdi_from_certificate", "sfdi_from_lfdi"]


def lfdi_from_certificate(pem: bytes) -> str:
    der = x509.load_pem_x509_certificate(pem).public_bytes(Encoding.DER)
    return hashlib.sha256(der).hexdigest()[:40].upper()


def sfdi_from_lfdi(lfdi: str) -> int:
    base = int(lfdi[:9], 16)  # 36 bits = 9 hex digits
    digit_sum = sum(int(d) for d in str(base))
    check = (10 - digit_sum % 10) % 10
    return base * 10 + check
