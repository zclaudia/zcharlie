import json

import pytest

from investment_agent.simulation.artifacts import ArtifactStore, safe_export


def test_content_addressing_verifies_reads_and_rejects_arbitrary_paths(tmp_path):
    store = ArtifactStore(tmp_path)
    path, digest = store.put({"cash": "0.100000000000000001"})
    assert json.loads(store.read(path, expected_hash=digest))["cash"] == "0.100000000000000001"
    assert store.put({"cash": "0.100000000000000001"}) == (path, digest)
    for bad in ("../secret", str(tmp_path / path), "objects/../../secret"):
        with pytest.raises(ValueError, match="reference"):
            store.read(bad, expected_hash=digest)
    (store.root / path).write_text("{}")
    with pytest.raises(ValueError, match="hash"):
        store.read(path, expected_hash=digest)


def test_symlink_cannot_escape_artifact_root(tmp_path):
    store = ArtifactStore(tmp_path)
    (tmp_path / "outside.json").write_text("{}")
    digest = "a" * 64
    (store.objects / f"{digest}.json").symlink_to(tmp_path / "outside.json")
    with pytest.raises(ValueError, match="outside"):
        store.read(f"objects/{digest}.json", expected_hash=digest)


def test_export_redacts_nested_credentials_and_authenticated_urls():
    value = safe_export(
        {
            "a": [{"apiKey": "secret", "plain": "cash"}],
            "url": "https://user:password@example.test/data?access_token=secret&day=1",
            "header": "Bearer never-export",
        }
    )
    assert value["a"][0]["apiKey"] == "[redacted]"
    assert value["a"][0]["plain"] == "cash"
    assert "password" not in value["url"] and "secret" not in value["url"]
    assert value["header"] == "[redacted]"
