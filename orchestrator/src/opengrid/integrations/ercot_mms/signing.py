"""WS-Security X.509 message signing for ERCOT EWS (protocol-adapters.md S6.3).

ERCOT authenticates EWS calls twice: mutual TLS with the QSE's ERCOT-issued client certificate, and a
WS-Security signature inside the SOAP header (EWS Appendix D): a `wsse:BinarySecurityToken` carrying
the X.509 certificate, a `wsu:Timestamp`, and a `ds:Signature` whose references cover the SOAP Body and
the Timestamp, canonicalized with Exclusive XML Canonicalization 1.0.

`X509WsSecuritySigner` builds exactly that, using lxml's exclusive C14N and `cryptography` for the RSA
signature. The digest/signature algorithms are configurable because the public appendix shows
RSA-SHA1/SHA1 while current practice is RSA-SHA256/SHA256; which one ERCOT's gateway accepts today is
confirmed in market trials (MOTE) at onboarding. `NullSigner` is for the simulator only.

The signer never logs key material; the private key file is read once at construction.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding
from lxml import etree  # type: ignore[import-untyped]

__all__ = [
    "DS_NS",
    "WSSE_NS",
    "WSU_NS",
    "MessageSigner",
    "NullSigner",
    "X509WsSecuritySigner",
    "exc_c14n",
]

WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
_X509_TOKEN = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3"  # noqa: S105
_BASE64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
_SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

_ALGORITHMS: dict[str, tuple[str, str, Any]] = {
    "sha256": (
        "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
        "http://www.w3.org/2001/04/xmlenc#sha256",
        hashes.SHA256,
    ),
    "sha1": (
        "http://www.w3.org/2000/09/xmldsig#rsa-sha1",
        "http://www.w3.org/2000/09/xmldsig#sha1",
        hashes.SHA1,
    ),
}


class MessageSigner(Protocol):
    def sign(self, envelope: Any) -> None:
        """Add the security header to the lxml SOAP envelope in place."""
        ...


class NullSigner:
    """No message signature (simulator only; ERCOT rejects unsigned EWS requests)."""

    def sign(self, envelope: Any) -> None:
        return None


def exc_c14n(element: Any) -> bytes:
    """Exclusive XML Canonicalization 1.0 (no comments) of one element."""
    data: bytes = etree.tostring(element, method="c14n", exclusive=True, with_comments=False)
    return data


class X509WsSecuritySigner:
    """WS-Security X.509 signer (module docstring)."""

    def __init__(
        self,
        cert_pem: bytes,
        key_pem: bytes,
        *,
        key_password: bytes | None = None,
        algorithm: Literal["sha256", "sha1"] = "sha256",
        timestamp_ttl_s: int = 300,
        clock: Any = None,
    ) -> None:
        self._cert = x509.load_pem_x509_certificate(cert_pem)
        key = serialization.load_pem_private_key(key_pem, password=key_password)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("ERCOT EWS signing needs an RSA private key")
        self._key = key
        self._sig_uri, self._digest_uri, self._hash = _ALGORITHMS[algorithm]
        self._ttl = timedelta(seconds=timestamp_ttl_s)
        self._clock = clock or (lambda: datetime.now(UTC))

    def _digest(self, element: Any) -> str:
        return base64.b64encode(hashlib.new(self._hash.name, exc_c14n(element)).digest()).decode()

    def sign(self, envelope: Any) -> None:
        header = envelope.find(f"{{{_SOAP_NS}}}Header")
        body = envelope.find(f"{{{_SOAP_NS}}}Body")
        if header is None or body is None:
            raise ValueError("SOAP envelope needs Header and Body")
        wsu_id = f"{{{WSU_NS}}}Id"
        body.set(wsu_id, f"Body-{uuid.uuid4().hex}")
        security = etree.SubElement(header, f"{{{WSSE_NS}}}Security", nsmap={"wsse": WSSE_NS, "wsu": WSU_NS})
        security.set(f"{{{_SOAP_NS}}}mustUnderstand", "1")
        token_id = f"X509-{uuid.uuid4().hex}"
        token = etree.SubElement(security, f"{{{WSSE_NS}}}BinarySecurityToken")
        token.set("EncodingType", _BASE64)
        token.set("ValueType", _X509_TOKEN)
        token.set(wsu_id, token_id)
        token.text = base64.b64encode(self._cert.public_bytes(Encoding.DER)).decode()
        now = self._clock()
        timestamp = etree.SubElement(security, f"{{{WSU_NS}}}Timestamp")
        timestamp.set(wsu_id, f"TS-{uuid.uuid4().hex}")
        etree.SubElement(timestamp, f"{{{WSU_NS}}}Created").text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        etree.SubElement(timestamp, f"{{{WSU_NS}}}Expires").text = (now + self._ttl).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        signature = etree.SubElement(security, f"{{{DS_NS}}}Signature", nsmap={"ds": DS_NS})
        signed_info = etree.SubElement(signature, f"{{{DS_NS}}}SignedInfo")
        etree.SubElement(signed_info, f"{{{DS_NS}}}CanonicalizationMethod").set("Algorithm", EXC_C14N)
        etree.SubElement(signed_info, f"{{{DS_NS}}}SignatureMethod").set("Algorithm", self._sig_uri)
        for target in (body, timestamp):
            ref = etree.SubElement(signed_info, f"{{{DS_NS}}}Reference")
            ref.set("URI", f"#{target.get(wsu_id)}")
            transforms = etree.SubElement(ref, f"{{{DS_NS}}}Transforms")
            etree.SubElement(transforms, f"{{{DS_NS}}}Transform").set("Algorithm", EXC_C14N)
            etree.SubElement(ref, f"{{{DS_NS}}}DigestMethod").set("Algorithm", self._digest_uri)
            etree.SubElement(ref, f"{{{DS_NS}}}DigestValue").text = self._digest(target)
        raw = self._key.sign(exc_c14n(signed_info), padding.PKCS1v15(), self._hash())
        etree.SubElement(signature, f"{{{DS_NS}}}SignatureValue").text = base64.b64encode(raw).decode()
        key_info = etree.SubElement(signature, f"{{{DS_NS}}}KeyInfo")
        str_ref = etree.SubElement(key_info, f"{{{WSSE_NS}}}SecurityTokenReference")
        reference = etree.SubElement(str_ref, f"{{{WSSE_NS}}}Reference")
        reference.set("URI", f"#{token_id}")
        reference.set("ValueType", _X509_TOKEN)
