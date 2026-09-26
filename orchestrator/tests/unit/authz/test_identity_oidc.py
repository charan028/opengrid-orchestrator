"""`OidcIdentity`: JWT bearer validation against a locally generated RSA key pair's JWKS -- no network,
no real IdP. Also covers `oidc_config_from`'s config-selectable, off-by-default gate."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.requests import Request

from opengrid.authz.identity import OidcConfig, OidcError, OidcIdentity, oidc_config_from
from opengrid.platform.config import Config

ISSUER = "https://idp.example.test/"
AUDIENCE = "opengrid-api"
KID = "test-key-1"


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "method": "GET",
        "path": "/",
    }
    return Request(scope)


@pytest.fixture(scope="module")
def keypair() -> tuple[rsa.RSAPrivateKey, dict]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk["kid"] = KID
    public_jwk["use"] = "sig"
    public_jwk["alg"] = "RS256"
    return private_key, public_jwk


def _sign(private_key: rsa.RSAPrivateKey, claims: dict, *, kid: str = KID) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def _config(public_jwk: dict, **overrides: object) -> OidcConfig:
    defaults: dict[str, object] = {
        "issuer": ISSUER,
        "jwks": {"keys": [public_jwk]},
        "audience": AUDIENCE,
        "username_claim": "sub",
        "leeway_s": 5,
    }
    defaults.update(overrides)
    return OidcConfig(**defaults)  # type: ignore[arg-type]


def test_valid_token_resolves_the_username_claim(keypair) -> None:
    private_key, public_jwk = keypair
    provider = OidcIdentity(_config(public_jwk))
    now = int(time.time())
    token = _sign(private_key, {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "iat": now, "exp": now + 300})
    request = _request({"authorization": f"Bearer {token}"})
    assert provider.identify(request) == "alice"


def test_missing_bearer_header_is_none(keypair) -> None:
    _private_key, public_jwk = keypair
    provider = OidcIdentity(_config(public_jwk))
    assert provider.identify(_request({})) is None


def test_wrong_signature_is_rejected(keypair) -> None:
    _private_key, public_jwk = keypair
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    provider = OidcIdentity(_config(public_jwk))
    now = int(time.time())
    token = _sign(other_key, {"iss": ISSUER, "aud": AUDIENCE, "sub": "mallory", "iat": now, "exp": now + 300})
    request = _request({"authorization": f"Bearer {token}"})
    assert provider.identify(request) is None


def test_expired_token_is_rejected(keypair) -> None:
    private_key, public_jwk = keypair
    provider = OidcIdentity(_config(public_jwk))
    now = int(time.time())
    token = _sign(
        private_key, {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "iat": now - 600, "exp": now - 300}
    )
    request = _request({"authorization": f"Bearer {token}"})
    assert provider.identify(request) is None


def test_wrong_issuer_is_rejected(keypair) -> None:
    private_key, public_jwk = keypair
    provider = OidcIdentity(_config(public_jwk))
    now = int(time.time())
    token = _sign(
        private_key,
        {"iss": "https://not-the-idp.test/", "aud": AUDIENCE, "sub": "alice", "iat": now, "exp": now + 300},
    )
    request = _request({"authorization": f"Bearer {token}"})
    assert provider.identify(request) is None


def test_unknown_kid_is_rejected(keypair) -> None:
    private_key, public_jwk = keypair
    provider = OidcIdentity(_config(public_jwk))
    now = int(time.time())
    token = _sign(
        private_key,
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "iat": now, "exp": now + 300},
        kid="unknown-kid",
    )
    request = _request({"authorization": f"Bearer {token}"})
    assert provider.identify(request) is None


def test_oidc_config_from_is_none_when_disabled_by_default() -> None:
    cfg = Config({})
    assert oidc_config_from(cfg) is None


def test_oidc_config_from_raises_when_enabled_but_unconfigured() -> None:
    cfg = Config({"authz": {"oidc": {"enabled": True}}})
    with pytest.raises(OidcError):
        oidc_config_from(cfg)


def test_oidc_config_from_builds_config_when_enabled(keypair) -> None:
    _private_key, public_jwk = keypair
    cfg = Config(
        {
            "authz": {
                "oidc": {
                    "enabled": True,
                    "issuer": ISSUER,
                    "audience": AUDIENCE,
                    "jwks": {"keys": [public_jwk]},
                }
            }
        }
    )
    resolved = oidc_config_from(cfg)
    assert resolved is not None
    assert resolved.issuer == ISSUER
    assert resolved.audience == AUDIENCE
