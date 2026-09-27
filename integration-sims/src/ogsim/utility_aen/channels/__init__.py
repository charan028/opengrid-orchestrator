"""ogsim.utility_aen.channels -- the registry of `Channel` implementations. The sim config picks one with
`channel: <name>` and passes `channels.<name>` to its factory. Each transport registers ONE line here."""

from __future__ import annotations

from ogsim.utility_aen.channels import customer_api, grid_link
from ogsim.utility_aen.channels.base import (
    CallResult,
    CallSpec,
    Channel,
    ChannelError,
    ChannelFactory,
    ChannelSettings,
)

CHANNEL_FACTORIES: dict[str, ChannelFactory] = {
    customer_api.NAME: customer_api.build,
    grid_link.NAME: grid_link.build,
}


def build_channel(name: str, settings: ChannelSettings) -> Channel:
    """The configured channel, or `ChannelError` for an unregistered name (never a silent default)."""
    factory = CHANNEL_FACTORIES.get(name)
    if factory is None:
        raise ChannelError(f"unknown channel {name!r}; registered: {sorted(CHANNEL_FACTORIES)}")
    return factory(settings)


__all__ = [
    "CHANNEL_FACTORIES",
    "CallResult",
    "CallSpec",
    "Channel",
    "ChannelError",
    "ChannelFactory",
    "ChannelSettings",
    "build_channel",
]
