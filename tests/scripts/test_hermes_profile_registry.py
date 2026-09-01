"""The shared Hermes Profile registry is the only mapping source for scripts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "scripts" / "hermes_profile_registry.txt"


def _records() -> list[tuple[str, str, str, str]]:
    records = []
    for line_number, raw_line in enumerate(
        REGISTRY.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("|")
        assert len(fields) == 4, (line_number, line)
        records.append(tuple(fields))
    return records


def test_registry_has_real_profile_sources_and_unique_ids() -> None:
    records = _records()
    assert len(records) == 10
    assert len({record[0] for record in records}) == len(records)

    departments = [record for record in records if record[2] == "department"]
    liaisons = [record for record in records if record[2] == "liaison"]
    assert len(departments) == 8
    assert len(liaisons) == 2

    for profile, directory, kind, container in records:
        source_dir = ROOT / "departments" / directory / (
            "hermes" if kind == "department" else "hermes-liaison"
        )
        assert (source_dir / "config.yaml").is_file(), profile
        assert (source_dir / "SOUL.md").is_file(), profile
        if kind == "department":
            assert container != "-", profile
        else:
            assert kind == "liaison", (profile, kind)
            assert container == "-", profile


def test_scripts_consume_registry_instead_of_copying_profile_mappings() -> None:
    registry_name = "hermes_profile_registry.txt"
    for script_name in (
        "scripts/sync_hermes_profiles.sh",
        "scripts/check_hermes_profiles.py",
    ):
        source = (ROOT / script_name).read_text(encoding="utf-8")
        assert registry_name in source

    # Direct container-copy installation predates the mounted runtime-profile
    # contract and could overwrite /opt/data outside the canonical sync path.
    assert not (ROOT / "scripts/install_hermes_profile.sh").exists()
