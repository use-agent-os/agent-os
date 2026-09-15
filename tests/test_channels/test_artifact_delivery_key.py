"""``artifact_delivery_key`` identity — delivery is keyed on content *and* name.

``sha256`` is a first-class artifact field and was consulted first, so two
files with different names and matching bytes produced the same key and the
second was dropped by ``dedupe_artifacts_for_channel_delivery`` before any
logging or text fallback could mention it.
"""

from __future__ import annotations

from agentos.channels.artifact_delivery import (
    artifact_delivery_key,
    dedupe_artifacts_for_channel_delivery,
)

SHA = "a" * 64


def test_same_content_different_names_are_distinct() -> None:
    first = {"sha256": SHA, "name": "q1.csv"}
    second = {"sha256": SHA, "name": "q2.csv"}
    assert artifact_delivery_key(first) != artifact_delivery_key(second)


def test_same_content_same_name_is_a_duplicate() -> None:
    first = {"sha256": SHA, "name": "report.csv"}
    second = {"sha256": SHA, "name": "report.csv"}
    assert artifact_delivery_key(first) == artifact_delivery_key(second)


def test_dedupe_keeps_distinct_names_with_identical_content() -> None:
    """Two empty CSVs the user asked for by name are two deliveries."""
    artifacts = [
        {"sha256": SHA, "name": "q1.csv"},
        {"sha256": SHA, "name": "q2.csv"},
        {"sha256": SHA, "name": "q1.csv"},
    ]
    kept = dedupe_artifacts_for_channel_delivery(artifacts)
    assert [item["name"] for item in kept] == ["q1.csv", "q2.csv"]


def test_dedupe_still_collapses_true_duplicates() -> None:
    artifacts = [
        {"sha256": SHA, "name": "same.png"},
        {"sha256": SHA, "name": "same.png"},
    ]
    assert len(dedupe_artifacts_for_channel_delivery(artifacts)) == 1


def test_key_is_stable_across_calls() -> None:
    """channel_dispatch matches delivered against undelivered by this key."""
    artifact = {"sha256": SHA, "name": "report.csv", "id": "art-1"}
    assert artifact_delivery_key(artifact) == artifact_delivery_key(dict(artifact))


def test_falls_back_through_the_field_order() -> None:
    assert artifact_delivery_key({"path": "/tmp/a.bin"}).startswith("path:")
    assert artifact_delivery_key({"id": "art-1"}).startswith("id:")
    assert artifact_delivery_key({"name": "only.txt"}) == "name:only.txt"


def test_name_is_not_repeated_when_it_is_the_identity_field() -> None:
    assert artifact_delivery_key({"name": "only.txt"}).count("only.txt") == 1


def test_missing_name_still_yields_a_key() -> None:
    assert artifact_delivery_key({"sha256": SHA}) == f"sha256:{SHA}"


def test_non_string_name_is_ignored() -> None:
    assert artifact_delivery_key({"sha256": SHA, "name": 42}) == f"sha256:{SHA}"


def test_empty_artifact_has_no_key() -> None:
    assert artifact_delivery_key({}) == ""


def test_keyless_artifacts_are_never_deduped_together() -> None:
    """An artifact with no identity field must not collapse into another."""
    artifacts = [{}, {}]
    assert len(dedupe_artifacts_for_channel_delivery(artifacts)) == 2
