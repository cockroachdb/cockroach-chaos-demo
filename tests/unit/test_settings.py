from pathlib import Path

import pytest

from controlplane.settings import Settings


def test_defaults_match_the_spec():
    s = Settings.from_env({})
    assert s.crdb_version == "v26.4.1"
    assert s.preset == "aws"
    assert s.subnet == "10.77.0.0/16"
    assert s.network == "crdb-chaos"
    assert s.max_nodes == 15
    assert s.haproxy_container == "haproxy"
    assert s.time_until_store_dead == "1m15s"
    assert s.data_dir == Path("/data")
    assert s.presets_dir == Path("/app/presets")
    assert s.crdb_license is None and s.crdb_org is None


def test_environment_overrides_and_blank_values_fall_back():
    s = Settings.from_env({
        "CRDB_VERSION": "v25.4.14", "PRESET": "gcp", "MAX_NODES": "9",
        "CHAOS_SUBNET": "  ", "CRDB_LICENSE": "key", "CRDB_ORG": "Acme",
    })
    assert s.crdb_version == "v25.4.14"
    assert s.preset == "gcp"
    assert s.max_nodes == 9
    assert s.subnet == "10.77.0.0/16"
    assert (s.crdb_license, s.crdb_org) == ("key", "Acme")


def test_tokens_and_helper_image():
    s = Settings.from_env({})
    assert s.control_token is None and s.trigger_token is None
    assert s.helper_image == "crdb-chaos-control-plane:local" and s.panel_port == 8088
    s = Settings.from_env({"CONTROL_TOKEN": "a", "TRIGGER_TOKEN": "b", "PANEL_PORT": "9000"})
    assert (s.control_token, s.trigger_token, s.panel_port) == ("a", "b", 9000)


def test_deployment_model_defaults_to_multi_region():
    assert Settings.from_env({}).model == "multi-region"
    assert Settings.from_env({"DEPLOYMENT_MODEL": "single-region"}).model == "single-region"


def test_the_default_cockroachdb_version_is_the_same_everywhere():
    """The default is written in five places: the control plane's settings, the compose file, run.sh, the example
    environment file and the proof run. A cluster is built with whichever one applies, so they must agree."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    default = Settings.from_env({}).crdb_version
    found = {
        "docker-compose.yml": re.findall(r"CRDB_VERSION:-(v[\d.]+)\}", (root / "docker-compose.yml").read_text()),
        "run.sh": re.findall(r'CRDB_VERSION="\$\{CRDB_VERSION:-(v[\d.]+)\}"', (root / "run.sh").read_text()),
        ".env.example": re.findall(r"^# CRDB_VERSION=(v[\d.]+)$", (root / ".env.example").read_text(), flags=re.M),
        "tools/prove.py": sorted(set(re.findall(r'cockroach:(v[\d.]+)"|default_version="(v[\d.]+)"', (root / "tools" / "prove.py").read_text()))),
    }
    assert found["docker-compose.yml"] == [default] and found["run.sh"] == [default] and found[".env.example"] == [default]
    assert {v for pair in found["tools/prove.py"] for v in pair if v} == {default}


def test_rebuild_is_allowed_unless_an_instance_switches_it_off():
    """REBUILD=off is for a cluster other people use: a rebuild wipes every database on it."""
    assert Settings.from_env({}).rebuild is True
    assert Settings.from_env({"REBUILD": "on"}).rebuild is True
    assert Settings.from_env({"REBUILD": " OFF "}).rebuild is False


def test_a_misspelt_rebuild_setting_stops_the_panel_rather_than_leaving_rebuild_on():
    with pytest.raises(ValueError, match="REBUILD must be on or off"):
        Settings.from_env({"REBUILD": "of"})
