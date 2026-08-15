import json
import os

import pytest


class TestGetSetTmdbApiKey:
    def test_returns_none_when_unset(self, isolated_config):
        assert isolated_config.get_tmdb_api_key() is None

    def test_set_then_get_round_trips(self, isolated_config):
        isolated_config.set_tmdb_api_key("my-secret-key")
        assert isolated_config.get_tmdb_api_key() == "my-secret-key"

    def test_persists_to_disk(self, isolated_config):
        isolated_config.set_tmdb_api_key("persisted-key")
        assert os.path.exists(isolated_config.CONFIG_PATH)
        with open(isolated_config.CONFIG_PATH) as f:
            data = json.load(f)
        assert data["tmdb_api_key"] == "persisted-key"

    def test_env_var_takes_priority_over_file(self, isolated_config, monkeypatch):
        isolated_config.set_tmdb_api_key("file-key")
        monkeypatch.setenv("TMDB_API_KEY", "env-key")
        assert isolated_config.get_tmdb_api_key() == "env-key"

    def test_strips_whitespace_on_set(self, isolated_config):
        isolated_config.set_tmdb_api_key("  spacey-key  \n")
        assert isolated_config.get_tmdb_api_key() == "spacey-key"

    def test_corrupt_config_file_treated_as_empty(self, isolated_config):
        os.makedirs(isolated_config.CONFIG_DIR, exist_ok=True)
        with open(isolated_config.CONFIG_PATH, "w") as f:
            f.write("{not valid json")
        assert isolated_config.get_tmdb_api_key() is None


class TestMaskKey:
    def test_masks_long_key(self, isolated_config):
        masked = isolated_config.mask_key("abcdefgh12345678")
        assert masked.startswith("abcd")
        assert masked.endswith("5678")
        assert "…" in masked

    def test_short_key_fully_masked(self, isolated_config):
        assert isolated_config.mask_key("abc") == "••••••••"

    def test_empty_key(self, isolated_config):
        assert isolated_config.mask_key("") == "••••••••"
