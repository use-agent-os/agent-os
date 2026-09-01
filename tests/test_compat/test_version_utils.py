"""Version-compare logic used by upgrade, update-notice, and skew policy."""

from __future__ import annotations

import pytest

from agentos.compat.version_utils import compare_versions, is_newer, parse_version


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("2026.7.18", "2026.7.18", 0),
        ("2026.7.18", "2026.7.19", -1),
        ("2026.7.19", "2026.7.18", 1),
        # post-release is newer than its base release
        ("2026.7.18.post1", "2026.7.18", 1),
        ("2026.7.18", "2026.7.18.post1", -1),
        ("2026.7.18.post2", "2026.7.18.post1", 1),
        # release-segment length differences pad with zeros
        ("2026.7", "2026.7.0", 0),
        ("2026.7.1", "2026.7", 1),
        # CalVer month/day rollover
        ("2026.10.1", "2026.7.30", 1),
        ("2027.1.1", "2026.12.31", 1),
        # pre-releases sort below the final release
        ("2026.7.18rc1", "2026.7.18", -1),
        ("2026.7.18a1", "2026.7.18b1", -1),
        ("2026.7.18rc2", "2026.7.18rc1", 1),
        # dev sorts below everything of the same release
        ("2026.7.18.dev1", "2026.7.18rc1", -1),
        ("2026.7.18.dev1", "2026.7.18", -1),
        # leading v is tolerated (case-insensitive)
        ("v2026.7.18", "2026.7.18", 0),
        ("V2026.7.18", "2026.7.18", 0),
        # case-insensitive pre-release, post-release, and dev tags
        ("2026.7.18RC1", "2026.7.18rc1", 0),
        ("2026.7.18-BETA2", "2026.7.18b2", 0),
        ("2026.7.18.POST1", "2026.7.18.post1", 0),
        ("2026.7.18.DEV1", "2026.7.18.dev1", 0),
        # bare dev sorts before final release
        ("2026.7.18.dev", "2026.7.18", -1),
        ("2026.7.18.DEV", "2026.7.18", -1),
        # dev pre-releases sort before the candidate (PEP 440)
        ("2026.7.18rc1.dev1", "2026.7.18rc1", -1),
    ],
)
def test_compare_versions(a: str, b: str, expected: int) -> None:
    assert compare_versions(a, b) == expected


def test_local_label_ignored_for_ordering() -> None:
    assert compare_versions("2026.7.18+abc", "2026.7.18") == 0


def test_unparsable_sorts_below_real_release() -> None:
    assert compare_versions("0.0.0+unknown", "2026.7.18") == -1
    assert compare_versions("garbage", "2026.7.18") == -1
    # an unparsable string equals itself
    assert compare_versions("garbage", "garbage") == 0


def test_is_newer() -> None:
    assert is_newer("2026.7.19", "2026.7.18") is True
    assert is_newer("V2026.7.19", "2026.7.18") is True
    assert is_newer("2026.7.18", "2026.7.18") is False
    assert is_newer("2026.7.18", "2026.7.19") is False
    assert is_newer("2026.7.18.post1", "2026.7.18") is True
    assert is_newer("2026.7.18.POST1", "2026.7.18") is True
    assert is_newer("2026.7.18", "2026.7.18.dev") is True


def test_parse_version_fields() -> None:
    v = parse_version("2026.7.18.post1")
    assert v.release == (2026, 7, 18)
    assert v.post == 1
    assert v.parsed is True

    v_upper = parse_version("V2026.7.18RC2.POST1.DEV0")
    assert v_upper.release == (2026, 7, 18)
    assert v_upper.pre == (2, 2)
    assert v_upper.post == 1
    assert v_upper.dev == 0
    assert v_upper.parsed is True

    bad = parse_version("nonsense")
    assert bad.parsed is False


def test_ordering_is_total_and_sortable() -> None:
    versions = [
        "2026.7.18",
        "2026.7.18.post1",
        "2026.7.18rc1",
        "2026.7.19",
        "0.0.0+unknown",
        "V2026.7.18.DEV1",
        "2026.7.18RC1.DEV1",
    ]
    ordered = sorted(versions, key=parse_version)
    assert ordered == [
        "0.0.0+unknown",
        "V2026.7.18.DEV1",
        "2026.7.18RC1.DEV1",
        "2026.7.18rc1",
        "2026.7.18",
        "2026.7.18.post1",
        "2026.7.19",
    ]
