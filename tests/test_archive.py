"""归档事件日志、修订历史、tombstone 与时点重建测试。"""
import tempfile
import unittest
from pathlib import Path

from app.atom import Link, ParsedEntry, ParsedTombstone
from app.collector import Ingester
from app.config import load_config
from app.model import (
    Archive,
    STATE_DELETED,
    STATE_LIVE,
    make_uid,
    parse_uid,
)

CONFIG = Path(__file__).resolve().parent.parent / "fixtures" / "offline" / "sources.json"
T1 = "2026-11-09T10:00:00Z"
T2 = "2026-11-10T10:00:00Z"
T3 = "2026-11-11T10:00:00Z"


def entry(entry_id, title, updated, summary="", content="", links=()):
    return ParsedEntry(
        entry_id=entry_id, title=title, updated=updated,
        published="", summary=summary, content=content,
        links=tuple(Link(href=h, rel=r) for h, r in links),
    )


class ArchiveIngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = Archive(Path(self.tmp.name) / "arc")
        self.ingester = Ingester(self.archive, load_config(CONFIG))

    def tearDown(self):
        self.tmp.cleanup()

    def test_revision_chain_preserves_summary_then_backfill(self):
        self.ingester.ingest(
            "k", "pub-a",
            type("F", (), {"entries": (entry("urn:a:1", "摘要版", "2026-11-09T09:00:00Z",
                                              summary="仅有摘要"),),
                           "tombstones": ()})(),
            1, T1,
        )
        self.ingester.ingest(
            "k", "pub-a",
            type("F", (), {"entries": (entry("urn:a:1", "全文版", "2026-11-10T09:00:00Z",
                                              summary="摘要", content="补发原文"),),
                           "tombstones": ()})(),
            2, T2,
        )
        entity = self.archive.get("pub-a", "urn:a:1")
        self.assertEqual(len(entity.revisions), 2)
        self.assertEqual(entity.revisions[0].summary, "仅有摘要")
        self.assertEqual(entity.revisions[0].content, "")
        self.assertEqual(entity.revisions[1].content, "补发原文")
        self.assertEqual(entity.state, STATE_LIVE)

    def test_unchanged_content_does_not_create_revision(self):
        payload = entry("urn:a:1", "同内容", "2026-11-09T09:00:00Z", summary="s")
        feed = type("F", (), {"entries": (payload,), "tombstones": ()})()
        self.ingester.ingest("k", "pub-a", feed, 1, T1)
        self.ingester.ingest("k", "pub-a", feed, 2, T2)
        entity = self.archive.get("pub-a", "urn:a:1")
        self.assertEqual(len(entity.revisions), 1)

    def test_tombstone_keeps_entity_and_marks_deleted(self):
        self.ingester.ingest(
            "k", "pub-a",
            type("F", (), {"entries": (entry("urn:a:1", "t", "2026-11-09T09:00:00Z"),),
                           "tombstones": ()})(),
            1, T1,
        )
        self.ingester.ingest(
            "k", "pub-a",
            type("F", (), {"entries": (),
                           "tombstones": (ParsedTombstone("urn:a:1", "2026-11-10T08:00:00Z"),)})(),
            2, T2,
        )
        entity = self.archive.get("pub-a", "urn:a:1")
        self.assertTrue(entity.is_deleted)
        self.assertEqual(len(entity.deletions), 1)
        self.assertEqual(entity.deletions[0].declared_when, "2026-11-10T08:00:00Z")
        # 历史修订仍保留
        self.assertEqual(len(entity.revisions), 1)

    def test_tombstone_first_sight_keeps_deleted_only_entity(self):
        self.ingester.ingest(
            "k", "pub-a",
            type("F", (), {"entries": (),
                           "tombstones": (ParsedTombstone("urn:a:ghost", "2026-11-10T08:00:00Z"),)})(),
            1, T1,
        )
        entity = self.archive.get("pub-a", "urn:a:ghost")
        self.assertIsNotNone(entity)
        self.assertTrue(entity.is_deleted)
        self.assertEqual(len(entity.revisions), 0)
        self.assertEqual(len(entity.deletions), 1)

    def test_reload_from_event_log_reproduces_state(self):
        self.ingester.ingest(
            "k", "pub-a",
            type("F", (), {"entries": (entry("urn:a:1", "v1", "2026-11-09T09:00:00Z"),),
                           "tombstones": ()})(),
            1, T1,
        )
        self.ingester.ingest(
            "k", "pub-a",
            type("F", (), {"entries": (entry("urn:a:1", "v2", "2026-11-10T09:00:00Z"),),
                           "tombstones": ()})(),
            2, T2,
        )
        reloaded = Archive(Path(self.tmp.name) / "arc")
        entity = reloaded.get("pub-a", "urn:a:1")
        self.assertEqual([r.title for r in entity.revisions], ["v1", "v2"])

    def test_point_in_time_views(self):
        for run_id, ts, title in (
            (1, T1, "v1"), (2, T2, "v2"), (3, T3, "v3"),
        ):
            self.ingester.ingest(
                "k", "pub-a",
                type("F", (), {"entries": (entry("urn:a:1", title, ts),),
                               "tombstones": ()})(),
                run_id, ts,
            )
        view_r1 = self.archive.view_at_run(1)
        self.assertEqual(view_r1.get("pub-a", "urn:a:1").latest.title, "v1")
        view_r2 = self.archive.view_at_run(2)
        self.assertEqual(view_r2.get("pub-a", "urn:a:1").latest.title, "v2")
        # 时间点视图：落在 T1 与 T2 之间只能看到 v1
        view_time = self.archive.view_at_time("2026-11-09T12:00:00Z")
        self.assertEqual(view_time.get("pub-a", "urn:a:1").latest.title, "v1")
        # 当前视图仍是 v3
        self.assertEqual(self.archive.get("pub-a", "urn:a:1").latest.title, "v3")

    def test_view_at_unknown_run_raises(self):
        from app.model import RunRecord
        self.archive.record_run(RunRecord(run_id=1, observed_at=T1))
        with self.assertRaises(KeyError):
            self.archive.view_at_run(99)

    def test_historical_view_excludes_future_generations(self):
        from app.model import Entity, Revision, make_uid
        # 直接构造三代实体的事件序列：每代一次 revision + 代间 split
        for gen in (1, 2, 3):
            uid = make_uid("pub-a", "urn:a:reuse", gen)
            ent = Entity(uid=uid, publisher_id="pub-a",
                         entry_id="urn:a:reuse", generation=gen)
            self.archive.entities[uid] = ent
            self.archive.record_revision(
                ent,
                Revision(seq=1, observed_at=T1, declared_updated=T1,
                         declared_published="", title=f"g{gen}",
                         summary="", content=""),
                run_id=gen, observed_at=T1,
            )
            if gen > 1:
                self.archive.record_split(
                    ent, make_uid("pub-a", "urn:a:reuse", gen - 1),
                    run_id=gen, observed_at=T2, reason="测试分裂")
        view_r2 = self.archive.view_at_run(2)
        gens = [e for e in view_r2.entities.values()
                if e.entry_id == "urn:a:reuse"]
        self.assertEqual(sorted(e.generation for e in gens), [1, 2])
        view_r1 = self.archive.view_at_run(1)
        gens = [e for e in view_r1.entities.values()
                if e.entry_id == "urn:a:reuse"]
        self.assertEqual([e.generation for e in gens], [1])

    def test_uid_roundtrip_and_namespace_isolation(self):
        uid = make_uid("pub-a", "urn:shared:1", 2)
        publisher, entry_id, gen = parse_uid(uid)
        self.assertEqual((publisher, entry_id, gen), ("pub-a", "urn:shared:1", 2))
        # 两个发布者使用相同 entry_id，实体互不覆盖
        self.ingester.ingest(
            "k1", "pub-a",
            type("F", (), {"entries": (entry("urn:shared:1", "A", "2026-11-09T09:00:00Z"),),
                           "tombstones": ()})(),
            1, T1,
        )
        self.ingester.ingest(
            "k2", "pub-b",
            type("F", (), {"entries": (entry("urn:shared:1", "B", "2026-11-09T09:00:00Z"),),
                           "tombstones": ()})(),
            1, T1,
        )
        self.assertEqual(self.archive.get("pub-a", "urn:shared:1").latest.title, "A")
        self.assertEqual(self.archive.get("pub-b", "urn:shared:1").latest.title, "B")
        self.assertEqual(
            len(self.archive.entities_by_publisher("pub-a")), 1
        )


if __name__ == "__main__":
    unittest.main()
