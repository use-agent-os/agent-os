"""Issue #2468: ``watch_rss`` reported the feed's own URL as an entry's link.

An Atom entry usually carries several ``<link>`` elements, and platforms
(GitHub Releases, WordPress, Reddit) commonly put ``rel="self"`` -- the feed's
own address -- before ``rel="alternate"``, the article. ``_entries`` took the
first ``<link>`` in document order, so every reported entry pointed at the
feed.

Selection is now by relation and type, per RFC 4287: ``alternate`` first (a
missing ``rel`` means ``alternate``), an HTML alternate ahead of a PDF one,
``self`` last of all. A relative ``href`` is resolved against ``xml:base``
and the feed URL, and an RSS item whose only URL is a permalink ``<guid>``
gets that as its link.
"""

from __future__ import annotations

import importlib.util
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "src/agentos/skills/bundled/cron-watchers/scripts"


@pytest.fixture(scope="module")
def watch_rss() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("watch_rss_under_test", SCRIPTS / "watch_rss.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def link(href: str, rel: str | None = None, type: str | None = None, base: str = "") -> str:
    """An Atom ``<link>`` element."""
    attributes = "".join(
        f' {name}="{value}"'
        for name, value in (("rel", rel), ("type", type), ("xml:base", base), ("href", href))
        if value is not None and (name != "xml:base" or value)
    )
    return f"<link{attributes}/>"


SELF = link("https://x/feed", rel="self")


def atom(
    *links: str, id: str = "u", title: str = "", feed_base: str = "", entry_base: str = ""
) -> str:
    """An Atom feed with one entry carrying *links*."""
    feed_attr = f' xml:base="{feed_base}"' if feed_base else ""
    entry_attr = f' xml:base="{entry_base}"' if entry_base else ""
    title_el = f"<title>{title}</title>" if title else ""
    return (
        f'<feed xmlns="http://www.w3.org/2005/Atom"{feed_attr}>'
        f"<entry{entry_attr}><id>{id}</id>{title_el}{''.join(links)}</entry></feed>"
    )


def rss(*inner: str) -> str:
    return f'<rss xmlns:atom="http://www.w3.org/2005/Atom"><channel><item>{"".join(inner)}</item></channel></rss>'


def entries(watch_rss: ModuleType, xml: str, feed_url: str = "") -> list[tuple[str, str, str]]:
    return watch_rss._entries(ET.fromstring(xml), feed_url=feed_url)


def link_of(watch_rss: ModuleType, xml: str, feed_url: str = "") -> str:
    return entries(watch_rss, xml, feed_url)[0][2]


# ── the issue ───────────────────────────────────────────────────────────────


def test_the_issues_feed_yields_the_article_not_the_feed(watch_rss: ModuleType) -> None:
    xml = atom(
        link("https://example.com/feed/atom", rel="self"),
        link("https://example.com/posts/first-post", rel="alternate", type="text/html"),
        id="urn:uuid:1234",
        title="First Post",
    )

    assert entries(watch_rss, xml) == [
        ("urn:uuid:1234", "First Post", "https://example.com/posts/first-post")
    ]


def test_a_missing_rel_means_alternate(watch_rss: ModuleType) -> None:
    assert link_of(watch_rss, atom(SELF, link("https://x/p/1"))) == "https://x/p/1"


# ── ranking ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("html_first", [True, False])
def test_an_html_alternate_outranks_a_pdf_one_whatever_the_order(
    watch_rss: ModuleType, html_first: bool
) -> None:
    html = link("https://x/p/1", rel="alternate", type="text/html")
    pdf = link("https://x/p/1.pdf", rel="alternate", type="application/pdf")
    xml = atom(html, pdf) if html_first else atom(pdf, html)

    assert link_of(watch_rss, xml) == "https://x/p/1"


def test_an_xhtml_alternate_counts_as_html(watch_rss: ModuleType) -> None:
    pdf = link("https://x/1.pdf", rel="alternate", type="application/pdf")
    xhtml = link("https://x/1", rel="alternate", type="application/xhtml+xml")

    assert link_of(watch_rss, atom(pdf, xhtml)) == "https://x/1"


def test_a_non_html_alternate_still_beats_an_enclosure(watch_rss: ModuleType) -> None:
    enclosure = link("https://x/ep.mp3", rel="enclosure")
    pdf = link("https://x/1.pdf", rel="alternate", type="application/pdf")

    assert link_of(watch_rss, atom(enclosure, pdf)) == "https://x/1.pdf"


def test_self_is_the_last_resort_behind_every_other_relation(watch_rss: ModuleType) -> None:
    """The feed's own address can never be an entry's destination."""
    xml = atom(SELF, link("https://x/ep1.mp3", rel="enclosure"))

    assert link_of(watch_rss, xml) == "https://x/ep1.mp3"


def test_self_alone_is_still_returned_rather_than_nothing(watch_rss: ModuleType) -> None:
    assert link_of(watch_rss, atom(SELF)) == "https://x/feed"


@pytest.mark.parametrize("rel", ["related", "via", "edit", "replies", "license"])
def test_other_relations_rank_between_alternate_and_self(watch_rss: ModuleType, rel: str) -> None:
    xml = atom(SELF, link("https://x/other", rel=rel), link("https://x/p/1", rel="alternate"))

    assert link_of(watch_rss, xml) == "https://x/p/1"


def test_the_first_of_equally_ranked_links_wins(watch_rss: ModuleType) -> None:
    xml = atom(link("https://x/a", rel="alternate"), link("https://x/b", rel="alternate"))

    assert link_of(watch_rss, xml) == "https://x/a"


# ── how rel is spelled ──────────────────────────────────────────────────────


def test_rel_is_case_insensitive(watch_rss: ModuleType) -> None:
    xml = atom(link("https://x/feed", rel="Self"), link("https://x/p/1", rel="Alternate"))

    assert link_of(watch_rss, xml) == "https://x/p/1"


def test_rel_may_be_the_iana_iri(watch_rss: ModuleType) -> None:
    """RFC 4287 §4.2.7.2 allows the registered relation as a full IRI."""
    iri = "http://www.iana.org/assignments/relation/alternate"

    assert link_of(watch_rss, atom(SELF, link("https://x/p/1", rel=iri))) == "https://x/p/1"


def test_an_empty_href_is_skipped_for_a_later_one(watch_rss: ModuleType) -> None:
    xml = atom(link("", rel="alternate"), link("https://x/p/1", rel="alternate"))

    assert link_of(watch_rss, xml) == "https://x/p/1"


def test_whitespace_around_an_href_is_stripped(watch_rss: ModuleType) -> None:
    assert link_of(watch_rss, atom(link("  https://x/p/1  "))) == "https://x/p/1"


# ── relative hrefs ──────────────────────────────────────────────────────────


def test_a_relative_href_resolves_against_xml_base_on_the_feed(watch_rss: ModuleType) -> None:
    xml = atom(link("posts/1", rel="alternate"), feed_base="https://x/blog/")

    assert link_of(watch_rss, xml) == "https://x/blog/posts/1"


def test_xml_base_on_the_entry_wins_over_the_feed(watch_rss: ModuleType) -> None:
    xml = atom(link("p1"), feed_base="https://x/blog/", entry_base="https://y/archive/")

    assert link_of(watch_rss, xml) == "https://y/archive/p1"


def test_nested_relative_xml_bases_chain_outward(watch_rss: ModuleType) -> None:
    """Each ``xml:base`` is itself relative to the next one out."""
    xml = atom(link("p1"), feed_base="https://x/blog/", entry_base="2026/")

    assert link_of(watch_rss, xml) == "https://x/blog/2026/p1"


def test_xml_base_on_the_link_element_itself_applies(watch_rss: ModuleType) -> None:
    xml = atom(link("a.html", base="https://x/media/"))

    assert link_of(watch_rss, xml) == "https://x/media/a.html"


def test_without_xml_base_a_relative_href_resolves_against_the_feed_url(
    watch_rss: ModuleType,
) -> None:
    xml = atom(link("/posts/1"))

    assert link_of(watch_rss, xml, feed_url="https://x/feeds/atom.xml") == "https://x/posts/1"


def test_a_relative_rss_link_resolves_against_the_feed_url_too(watch_rss: ModuleType) -> None:
    xml = rss("<guid>g</guid><link>/p/1</link>")

    assert link_of(watch_rss, xml, feed_url="https://x/rss.xml") == "https://x/p/1"


def test_an_absolute_href_is_never_altered_by_a_base(watch_rss: ModuleType) -> None:
    xml = atom(link("https://x/p/1"), feed_base="https://other/")

    assert link_of(watch_rss, xml, feed_url="https://elsewhere/feed") == "https://x/p/1"


def test_no_feed_url_and_no_base_leaves_a_relative_href_as_written(
    watch_rss: ModuleType,
) -> None:
    assert link_of(watch_rss, atom(link("posts/1"))) == "posts/1"


# ── RSS ─────────────────────────────────────────────────────────────────────


def test_rss_link_text_is_used_as_before(watch_rss: ModuleType) -> None:
    xml = rss("<guid>g</guid><title>T</title><link>https://x/p/1</link>")

    assert entries(watch_rss, xml) == [("g", "T", "https://x/p/1")]


def test_rss_link_text_wins_over_an_atom_self_link_inside_the_item(
    watch_rss: ModuleType,
) -> None:
    xml = rss(
        "<guid>g</guid><link>https://x/p/1</link>",
        '<atom:link rel="self" href="https://x/feed"/>',
    )

    assert link_of(watch_rss, xml) == "https://x/p/1"


@pytest.mark.parametrize(
    "guid",
    ['<guid isPermaLink="true">https://x/p/1</guid>', "<guid>https://x/p/1</guid>"],
)
def test_an_rss_item_without_a_link_uses_its_permalink_guid(
    watch_rss: ModuleType, guid: str
) -> None:
    """``isPermaLink`` defaults to true in the RSS 2.0 spec."""
    assert link_of(watch_rss, rss(guid, "<title>T</title>")) == "https://x/p/1"


def test_a_non_permalink_guid_is_not_a_link(watch_rss: ModuleType) -> None:
    xml = rss('<guid isPermaLink="false">tag:x,2026:1</guid><title>T</title>')

    assert link_of(watch_rss, xml) == ""


def test_a_permalink_guid_that_is_not_a_url_is_not_a_link(watch_rss: ModuleType) -> None:
    assert link_of(watch_rss, rss("<guid>1</guid><title>T</title>")) == ""


def test_the_guid_stays_the_id_even_when_it_doubles_as_the_link(watch_rss: ModuleType) -> None:
    xml = rss("<guid>https://x/p/1</guid><title>T</title>")

    assert entries(watch_rss, xml) == [("https://x/p/1", "T", "https://x/p/1")]


# ── what must not change ────────────────────────────────────────────────────


def test_an_entry_with_no_link_at_all_still_appears_with_an_empty_link(
    watch_rss: ModuleType,
) -> None:
    assert entries(watch_rss, atom(title="T")) == [("u", "T", "")]


def test_multiple_entries_each_get_their_own_link(watch_rss: ModuleType) -> None:
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<entry><id>a</id>{SELF}{link('https://x/a')}</entry>"
        f"<entry><id>b</id>{SELF}{link('https://x/b')}</entry>"
        "</feed>"
    )

    assert [e[2] for e in entries(watch_rss, xml)] == ["https://x/a", "https://x/b"]


def test_the_id_falls_back_to_the_link(watch_rss: ModuleType) -> None:
    xml = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="https://x/only"/></entry></feed>'

    assert entries(watch_rss, xml)[0][0] == "https://x/only"


def test_a_feed_level_link_is_not_mistaken_for_an_entry(watch_rss: ModuleType) -> None:
    xml = (
        f'<feed xmlns="http://www.w3.org/2005/Atom">{SELF}'
        f"<entry><id>u</id>{link('https://x/p/1')}</entry></feed>"
    )

    assert len(entries(watch_rss, xml)) == 1
    assert link_of(watch_rss, xml) == "https://x/p/1"
