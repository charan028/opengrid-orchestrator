import pytest

from opengrid.platform.config import ConfigError, load_config


@pytest.fixture(autouse=True)
def _clean_workspace_env(monkeypatch):
    """BUILD.md S5: tools/remote.ps1 always sets OG_DB/OG_MQTT_ROOT for the calling workspace. These
    tests assert what `load_config` does with and without those overrides, so they must not inherit
    whatever the ambient shell (dev machine or a remote workspace) happens to have set."""
    monkeypatch.delenv("OG_DB", raising=False)
    monkeypatch.delenv("OG_MQTT_ROOT", raising=False)


TOML_BODY = """
[postgres]
host = "127.0.0.1"
database = "og"

[mqtt]
topic_root = "og/v1"
"""


def _write_toml(tmp_path, body=TOML_BODY):
    path = tmp_path / "orchestrator.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_config_from_explicit_path(tmp_path):
    path = _write_toml(tmp_path)
    cfg = load_config(path)
    assert cfg.postgres_database == "og"
    assert cfg.mqtt_topic_root == "og/v1"


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


def test_load_config_og_config_unset_raises_config_error(monkeypatch):
    """PLAT-001: with no explicit path and OG_CONFIG unset, `Path("")` used to normalise to the cwd
    (truthy, exists) so the guard silently fell through to opening the cwd as a file (PermissionError/
    IsADirectoryError) instead of the documented ConfigError."""
    monkeypatch.delenv("OG_CONFIG", raising=False)
    with pytest.raises(ConfigError):
        load_config(None)


def test_load_config_og_config_empty_string_raises_config_error(monkeypatch):
    monkeypatch.setenv("OG_CONFIG", "")
    with pytest.raises(ConfigError):
        load_config(None)


def test_og_db_env_overrides_database(tmp_path, monkeypatch):
    path = _write_toml(tmp_path)
    monkeypatch.setenv("OG_DB", "og_t_arch")
    cfg = load_config(path)
    assert cfg.postgres_database == "og_t_arch"


def test_og_mqtt_root_env_overrides_topic_root(tmp_path, monkeypatch):
    path = _write_toml(tmp_path)
    monkeypatch.setenv("OG_MQTT_ROOT", "ogtest/arch")
    cfg = load_config(path)
    assert cfg.mqtt_topic_root == "ogtest/arch"


def test_get_with_dotted_path_and_default(tmp_path):
    path = _write_toml(tmp_path)
    cfg = load_config(path)
    assert cfg.get("postgres.pool_min", 2) == 2
    assert cfg.get("nonexistent.path", "fallback") == "fallback"
