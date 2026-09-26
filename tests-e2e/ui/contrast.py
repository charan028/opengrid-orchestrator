"""Pure WCAG 2.x contrast helpers for the a11y e2e checks: sRGB hex -> relative luminance -> contrast
ratio (WCAG 2.1 "relative luminance" / "contrast ratio" definitions), plus a parser for the colour
tokens `opengrid.ui/static/og.css` defines per `data-theme`. No I/O; unit-tested in `test_contrast.py`."""

from __future__ import annotations

import re

AA_BODY_TEXT = 4.5
AA_LARGE_OR_UI = 3.0

_TOKEN_RE = re.compile(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\s*;")


def _channels(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        raise ValueError(f"not a #rgb/#rrggbb colour: {hex_color!r}")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance in [0, 1] of an sRGB colour."""

    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(ch) for ch in _channels(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(color_a: str, color_b: str) -> float:
    """WCAG contrast ratio in [1, 21]; symmetric in its arguments."""
    lum_a, lum_b = relative_luminance(color_a), relative_luminance(color_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def mix_srgb(color_a: str, color_b: str, weight_a: float) -> str:
    """Approximate CSS `color-mix(in srgb, a W%, b)` -- a per-channel linear blend in sRGB."""
    if not 0.0 <= weight_a <= 1.0:
        raise ValueError("weight_a must be within [0, 1]")
    a, b = _channels(color_a), _channels(color_b)
    mixed = (round(ca * weight_a + cb * (1 - weight_a)) for ca, cb in zip(a, b, strict=True))
    return "#" + "".join(f"{ch:02x}" for ch in mixed)


def parse_theme_tokens(css: str, theme: str) -> dict[str, str]:
    """Colour custom properties (`--name: #hex`) in effect for `[data-theme="<theme>"]`. The dark block
    doubles as `:root`, so a theme that omits a token inherits the dark value -- exactly what the browser
    does, and exactly the case that hides a missing override."""
    dark = _block_tokens(css, "dark")
    if theme == "dark":
        return dark
    return {**dark, **_block_tokens(css, theme)}


def _block_tokens(css: str, theme: str) -> dict[str, str]:
    match = re.search(r'\[data-theme="' + re.escape(theme) + r'"\][^{]*\{([^}]*)\}', css)
    if match is None:
        raise ValueError(f"no [data-theme={theme!r}] block in stylesheet")
    return {name: value.lower() for name, value in _TOKEN_RE.findall(match.group(1))}
