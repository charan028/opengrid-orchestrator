"""Unit test for the pure `contrast.py` helper (known WCAG reference values and the token parser)."""

from __future__ import annotations

import pytest

from contrast import contrast_ratio, mix_srgb, parse_theme_tokens, relative_luminance

_CSS = """
:root,
[data-theme="dark"] {
  --bg: #0b0e14;
  --text: #E6E9F0;
  --focus-ring: #7cc4ff;
  color-scheme: dark;
}

[data-theme="light"] {
  --bg: #fff;
  --text: #171a21;
}
"""


def test_luminance_of_black_and_white() -> None:
    assert relative_luminance("#000000") == 0.0
    assert relative_luminance("#ffffff") == pytest.approx(1.0)


def test_contrast_ratio_reference_values() -> None:
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast_ratio("#ffffff", "#ffffff") == pytest.approx(1.0)
    # the classic "#767676 on white is the darkest grey that just passes AA" reference pair
    assert contrast_ratio("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
    assert contrast_ratio("#777777", "#ffffff") < 4.5
    assert contrast_ratio("#ffffff", "#767676") == contrast_ratio("#767676", "#ffffff")


def test_mix_srgb_endpoints_and_midpoint() -> None:
    assert mix_srgb("#ff0000", "#0000ff", 1.0) == "#ff0000"
    assert mix_srgb("#ff0000", "#0000ff", 0.0) == "#0000ff"
    assert mix_srgb("#000000", "#ffffff", 0.5) == "#808080"
    with pytest.raises(ValueError, match="weight_a"):
        mix_srgb("#000000", "#ffffff", 1.5)


def test_parse_theme_tokens_light_inherits_from_dark() -> None:
    dark = parse_theme_tokens(_CSS, "dark")
    light = parse_theme_tokens(_CSS, "light")
    assert dark == {"bg": "#0b0e14", "text": "#e6e9f0", "focus-ring": "#7cc4ff"}
    assert light["bg"] == "#fff"
    assert light["text"] == "#171a21"
    assert light["focus-ring"] == "#7cc4ff"  # not overridden -> inherited from :root/dark
    with pytest.raises(ValueError, match="no \\[data-theme"):
        parse_theme_tokens(_CSS, "sepia")
