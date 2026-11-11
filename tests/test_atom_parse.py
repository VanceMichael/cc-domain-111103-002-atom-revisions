"""Atom 解析层测试：身份、摘要/原文、链接、声明时间与 tombstone。"""
import unittest

from app.atom import parse_feed, parse_entries


def x(raw: str) -> bytes:
    return raw.encode("utf-8")


class ParseFeedTests(unittest.TestCase):
    def test_entry_identity_and_declared_times(self):
        feed = parse_feed(x(
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><id>urn:p:x</id><title>t</title>"
            "<published>2026-11-01T00:00:00Z</published>"
            "<updated>2026-11-02T00:00:00Z</updated>"
            "<summary>摘要原文</summary></entry></feed>"
        ))
        entry = feed.entries[0]
        self.assertEqual(entry.entry_id, "urn:p:x")
        self.assertEqual(entry.published, "2026-11-01T00:00:00Z")
        self.assertEqual(entry.updated, "2026-11-02T00:00:00Z")
        self.assertEqual(entry.summary, "摘要原文")

    def test_summary_then_content_backfill(self):
        first = parse_feed(x(
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><id>urn:p:1</id><summary>仅摘要</summary></entry></feed>"
        ))
        second = parse_feed(x(
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><id>urn:p:1</id><summary>摘要</summary>"
            "<content>补发原文</content></entry></feed>"
        ))
        self.assertEqual(first.entries[0].content, "")
        self.assertEqual(second.entries[0].content, "补发原文")

    def test_links_keep_rel_and_href(self):
        feed = parse_feed(x(
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>urn:p:1</id>'
            '<link rel="canonical" href="https://origin.invalid/a" />'
            '<link href="https://mirror.invalid/a" /></entry></feed>'
        ))
        rels = {lk.rel: lk.href for lk in feed.entries[0].links}
        self.assertEqual(rels["canonical"], "https://origin.invalid/a")
        # 缺省 rel 按 Atom 规范视为 alternate
        self.assertEqual(rels["alternate"], "https://mirror.invalid/a")

    def test_tombstone_in_tombstone_namespace(self):
        feed = parse_feed(x(
            '<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:t="http://purl.org/atompub/tombstones/1.0">'
            '<t:deleted-entry ref="urn:p:old" when="2026-11-03T08:00:00Z" />'
            "</feed>"
        ))
        self.assertEqual(len(feed.entries), 0)
        self.assertEqual(feed.tombstones[0].ref, "urn:p:old")
        self.assertEqual(feed.tombstones[0].when, "2026-11-03T08:00:00Z")

    def test_tombstone_default_namespace_too(self):
        feed = parse_feed(x(
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            '<deleted-entry ref="urn:p:old2" when="2026-11-03T09:00:00Z" />'
            "</feed>"
        ))
        self.assertEqual(feed.tombstones[0].ref, "urn:p:old2")

    def test_missing_id_raises(self):
        with self.assertRaises(ValueError):
            parse_feed(x(
                '<feed xmlns="http://www.w3.org/2005/Atom">'
                "<entry><title>无 id</title></entry></feed>"
            ))

    def test_malformed_raises(self):
        with self.assertRaises(Exception):
            parse_feed(b"<feed>")

    def test_legacy_parse_entries_backward_compatible(self):
        entries = parse_entries(
            b'<feed xmlns="http://www.w3.org/2005/Atom">'
            b"<entry><id>a</id><title>x</title><updated>u</updated></entry>"
            b"<entry><id>b</id></entry></feed>"
        )
        self.assertEqual([e.entry_id for e in entries], ["a", "b"])
        self.assertEqual(entries[0].title, "x")
        self.assertEqual(entries[0].updated, "u")


if __name__ == "__main__":
    unittest.main()
