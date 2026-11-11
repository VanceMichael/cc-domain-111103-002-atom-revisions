"""采集编排测试：故障隔离、304 语义、ID 重用、地址迁移与转载证据。"""
import json
import tempfile
import unittest
from pathlib import Path

from app.collector import (
    Collector,
    OfflineFetcher,
    ResponseCache,
    ST_FAILED,
    ST_NOT_MODIFIED,
    ST_NOT_MODIFIED_NO_CACHE,
    ST_OK,
)
from app.config import load_config
from app.model import Archive, STATE_LIVE

FIXT = Path(__file__).resolve().parent.parent / "fixtures" / "offline"
T1 = "2026-11-09T10:00:00Z"
T2 = "2026-11-10T10:00:00Z"
T3 = "2026-11-11T10:00:00Z"


def script(round_no: int) -> dict:
    return json.loads((FIXT / f"round{round_no}" / "script.json").read_text(
        encoding="utf-8"))


class Harness:
    def __init__(self, config_path=FIXT / "sources.json"):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = load_config(config_path)
        self.archive = Archive(root / "archive")
        self.cache = ResponseCache(root / "cache")

    def run(self, payload, observed_at):
        fetcher = OfflineFetcher(payload, base_dir=FIXT)
        return Collector(self.config, self.archive, self.cache, fetcher).run(
            observed_at)

    def close(self):
        self.tmp.cleanup()


class FailureIsolationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_one_failed_feed_keeps_others_and_never_infers_deletes(self):
        self.h.run(script(1), T1)
        before = {u: e.state for u, e in self.h.archive.entities.items()}
        record = self.h.run(script(2), T2)
        statuses = {fs.feed_key: fs.status for fs in record.feeds}
        self.assertEqual(statuses["wire"], ST_FAILED)
        self.assertEqual(statuses["association"], ST_OK)
        self.assertEqual(statuses["city"], ST_NOT_MODIFIED)

        # 故障 feed 的旧条目原样保留，没有被误判为删除
        wire_entity = self.h.archive.get(
            "wire.example", "urn:wire.example:item-7781")
        self.assertEqual(wire_entity.state, STATE_LIVE)
        self.assertEqual(len(wire_entity.deletions), 0)
        # 其他 feed 本轮的完整结果正常写入
        after = {u: e.state for u, e in self.h.archive.entities.items()}
        for uid, state in before.items():
            self.assertEqual(after[uid], state)

    def test_parse_failure_is_isolated_like_transport_failure(self):
        self.h.run(script(1), T1)
        payload = {
            "https://association.example/feed.atom": {
                "http_status": 200,
                "body_inline": "<feed><entry><id>broken",
            },
            "https://city.example/feed.atom": {"http_status": 304, "etag": "city-v1"},
            "https://wire.example/feed.atom": {"http_status": 304, "etag": "wire-v1"},
        }
        record = self.h.run(payload, T2)
        statuses = {fs.feed_key: fs.status for fs in record.feeds}
        self.assertEqual(statuses["association"], ST_FAILED)
        # 解析失败没有产生任何实体变更
        assoc = self.h.archive.get(
            "association.example", "urn:association.example:notice-2026-11")
        self.assertEqual(len(assoc.revisions), 1)


class NotModifiedTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_304_with_matching_cache_means_unchanged(self):
        self.h.run(script(1), T1)
        record = self.h.run(
            {
                "https://association.example/feed.atom": {"http_status": 304, "etag": "assoc-v1"},
                "https://city.example/feed.atom": {"http_status": 304, "etag": "city-v1"},
                "https://wire.example/feed.atom": {"http_status": 304, "etag": "wire-v1"},
            },
            T2,
        )
        self.assertTrue(
            all(fs.status == ST_NOT_MODIFIED for fs in record.feeds)
        )

    def test_304_without_local_cache_is_distinct_and_not_reused(self):
        # 没有任何历史缓存就收到 304
        record = self.h.run(
            {url: {"http_status": 304, "etag": "unknown"}
             for url in (
                 "https://association.example/feed.atom",
                 "https://city.example/feed.atom",
                 "https://wire.example/feed.atom",
             )},
            T1,
        )
        self.assertTrue(
            all(fs.status == ST_NOT_MODIFIED_NO_CACHE for fs in record.feeds)
        )
        self.assertEqual(len(self.h.archive.entities), 0)

    def test_304_with_stale_validator_after_cache_loss_is_distinct(self):
        self.h.run(script(1), T1)
        self.h.cache.discard("city")  # 模拟该 feed 缓存丢失
        record = self.h.run(
            {
                "https://association.example/feed.atom": {"http_status": 304, "etag": "assoc-v1"},
                "https://city.example/feed.atom": {"http_status": 304, "etag": "city-stale-unknown"},
                "https://wire.example/feed.atom": {"http_status": 304, "etag": "wire-v1"},
            },
            T2,
        )
        statuses = {fs.feed_key: fs.status for fs in record.feeds}
        self.assertEqual(statuses["city"], ST_NOT_MODIFIED_NO_CACHE)
        self.assertEqual(statuses["association"], ST_NOT_MODIFIED)
        self.assertEqual(statuses["wire"], ST_NOT_MODIFIED)
        # city 归档实体不受影响，且没有任何写入
        city = self.h.archive.get(
            "city.example", "urn:city.example:bulletin-305")
        self.assertEqual(len(city.revisions), 1)


    def test_304_with_mismatched_validator_is_treated_as_unresolvable(self):
        self.h.run(script(1), T1)
        record = self.h.run(
            {
                "https://association.example/feed.atom": {
                    "http_status": 304, "etag": "assoc-v1"},
                "https://city.example/feed.atom": {
                    "http_status": 304, "etag": "totally-different-etag"},
                "https://wire.example/feed.atom": {
                    "http_status": 304, "last_modified": "Wed, 11 Nov 2099 00:00:00 GMT"},
            },
            T2,
        )
        statuses = {fs.feed_key: fs.status for fs in record.feeds}
        self.assertEqual(statuses["association"], ST_NOT_MODIFIED)
        self.assertEqual(statuses["city"], ST_NOT_MODIFIED_NO_CACHE)
        self.assertEqual(statuses["wire"], ST_NOT_MODIFIED_NO_CACHE)

    def test_304_without_any_validator_is_unresolvable(self):
        self.h.run(script(1), T1)
        record = self.h.run(
            {url: {"http_status": 304}
             for url in (
                 "https://association.example/feed.atom",
                 "https://city.example/feed.atom",
                 "https://wire.example/feed.atom",
             )},
            T2,
        )
        self.assertTrue(
            all(fs.status == ST_NOT_MODIFIED_NO_CACHE for fs in record.feeds)
        )


class IdReuseTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_default_split_policy_keeps_old_record_off_new_entity(self):
        self.h.run(script(1), T1)
        self.h.run(script(2), T2)  # notice-2026-10 被 tombstone
        self.h.run(script(3), T3)  # 同 ID 重新出现 -> 默认分裂为 gen2
        generations = self.h.archive.all_generations(
            "association.example", "urn:association.example:notice-2026-10")
        self.assertEqual([g.generation for g in generations], [1, 2])
        gen1, gen2 = generations
        self.assertTrue(gen1.is_deleted)
        self.assertEqual(gen2.state, STATE_LIVE)
        self.assertEqual(gen2.predecessor_uid, gen1.uid)
        self.assertTrue(gen2.split_reason)
        # 旧实体的撤回记录没有被嫁接带走
        self.assertEqual(len(gen1.deletions), 1)
        self.assertEqual(len(gen2.deletions), 0)

    def test_round5_reuse_after_repeated_tombstone_makes_gen3(self):
        for round_no, ts in ((1, T1), (2, T2), (3, T3)):
            self.h.run(script(round_no), ts)
        self.h.run(script(5), "2026-11-11T15:00:00Z")
        generations = self.h.archive.all_generations(
            "association.example", "urn:association.example:notice-2026-10")
        self.assertEqual([g.generation for g in generations], [1, 2, 3])
        latest = self.h.archive.get(
            "association.example", "urn:association.example:notice-2026-10")
        self.assertEqual(latest.generation, 3)
        self.assertEqual(latest.latest.title, "完全不同的虚构年度统计通告")


class FeedMigrationTests(unittest.TestCase):
    def test_migration_keeps_namespace_identity(self):
        # 迁移前：当前地址是 old.*；迁移后：当前地址切换为新地址，
        # old.* 登记在 migrated_from。两者 publisher_id 不变。
        h = Harness(FIXT / "sources_pre_migration.json")
        try:
            h.run({
                "https://old.association.example/feed.atom": {
                    "http_status": 200, "body_file": "round1/association.xml",
                    "etag": "assoc-v1",
                },
                "https://city.example/feed.atom": {
                    "http_status": 200, "body_file": "round1/city.xml",
                    "etag": "city-v1",
                },
                "https://wire.example/feed.atom": {
                    "http_status": 200, "body_file": "round1/wire.xml",
                    "etag": "wire-v1",
                },
            }, T1)
            entity_before = h.archive.get(
                "association.example",
                "urn:association.example:notice-2026-11")
            self.assertIsNotNone(entity_before)

            # 切换到迁移后的配置，复用同一归档
            h.config = load_config(FIXT / "sources_migrated.json")
            h.run({
                "https://association.example/feed.atom": {
                    "http_status": 200, "body_file": "round2/association.xml",
                    "etag": "assoc-v2",
                },
                "https://city.example/feed.atom": {"http_status": 304, "etag": "city-v1"},
                "https://wire.example/feed.atom": {"http_status": 304, "etag": "wire-v1"},
            }, T2)
            entity_after = h.archive.get(
                "association.example",
                "urn:association.example:notice-2026-11")
            self.assertEqual(entity_after.uid, entity_before.uid)
            self.assertEqual(len(entity_after.revisions), 2)
            self.assertTrue(h.config.is_known_url(
                "https://old.association.example/feed.atom"))
        finally:
            h.close()


class RelationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_canonical_link_confirms_repost_relation(self):
        self.h.run(script(1), T1)
        relations = self.h.archive.relations
        self.assertEqual(len(relations), 1)
        rel = relations[0]
        self.assertTrue(rel.source_uid.startswith("city.example"))
        self.assertTrue(rel.target_uid.startswith("wire.example"))
        self.assertEqual(rel.evidence, "canonical-link")

    def test_same_title_across_publishers_is_only_candidate(self):
        self.h.run(script(1), T1)
        candidates = self.h.archive.candidates
        weak = [c for c in candidates if c.reason == "weak-title-match"]
        self.assertTrue(weak)
        # 弱匹配绝不产生确认关系，也不合并实体
        confirmed_pairs = {
            (r.source_uid.split("!")[0], r.target_uid.split("!")[0])
            for r in self.h.archive.relations
        }
        self.assertNotIn(
            ("association.example", "city.example"), confirmed_pairs)
        self.assertEqual(
            len(self.h.archive.entities_by_publisher("association.example")), 1)

    def test_relations_and_candidates_are_deduped_across_runs(self):
        self.h.run(script(1), T1)
        self.h.run(script(1), T2)  # 同样内容再采一轮
        self.h.run(script(1), T3)
        canonical = [r for r in self.h.archive.relations
                     if r.evidence == "canonical-link"]
        weak = [c for c in self.h.archive.candidates
                if c.reason == "weak-title-match"]
        self.assertEqual(len(canonical), 1)
        self.assertEqual(len(weak), 1)

    def test_canonical_target_unresolved_becomes_candidate(self):
        payload = {
            "https://association.example/feed.atom": {
                "http_status": 200,
                "body_inline": (
                    '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                    "<id>urn:association.example:x</id><title>t</title>"
                    '<link rel="canonical" href="https://missing.invalid/x" />'
                    "</entry></feed>"),
            },
            "https://city.example/feed.atom": {"error": "down"},
            "https://wire.example/feed.atom": {"error": "down"},
        }
        self.h.run(payload, T1)
        cand = [c for c in self.h.archive.candidates
                if c.reason == "canonical-target-unresolved"]
        self.assertEqual(len(cand), 1)
        self.assertEqual(self.h.archive.relations, [])


class ContinuePolicyTests(unittest.TestCase):
    """continue 策略：证据齐备时撤回后恢复同一实体，不足时仍分裂。"""

    def _harness(self):
        return Harness(FIXT / "sources_continue.json")

    ENTRY_V1 = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<id>urn:association.example:n</id><title>同一虚构通知</title>"
        '<updated>2026-11-09T09:00:00Z</updated><summary>v1</summary>'
        '<link rel="alternate" href="https://association.example/n" />'
        "</entry></feed>"
    )

    def test_revival_with_satisfied_evidence_reuses_entity(self):
        h = self._harness()
        try:
            h.run({
                "https://association.example/feed.atom": {
                    "http_status": 200, "body_inline": self.ENTRY_V1,
                    "etag": "v1"},
                "https://city.example/feed.atom": {"error": "down"},
                "https://wire.example/feed.atom": {"error": "down"},
            }, T1)
            h.run({
                "https://association.example/feed.atom": {
                    "http_status": 200,
                    "body_inline": (
                        '<feed xmlns="http://www.w3.org/2005/Atom" '
                        'xmlns:t="http://purl.org/atompub/tombstones/1.0">'
                        '<t:deleted-entry ref="urn:association.example:n" '
                        'when="2026-11-10T08:00:00Z" /></feed>'),
                    "etag": "v2"},
                "https://city.example/feed.atom": {"error": "down"},
                "https://wire.example/feed.atom": {"error": "down"},
            }, T2)
            # 撤回后恢复：标题与链接均保持，满足证据
            h.run({
                "https://association.example/feed.atom": {
                    "http_status": 200,
                    "body_inline": (
                        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                        "<id>urn:association.example:n</id>"
                        "<title>同一虚构通知</title>"
                        '<updated>2026-11-11T09:00:00Z</updated>'
                        "<summary>恢复版</summary>"
                        '<link rel="alternate" href="https://association.example/n" />'
                        "</entry></feed>"),
                    "etag": "v3"},
                "https://city.example/feed.atom": {"error": "down"},
                "https://wire.example/feed.atom": {"error": "down"},
            }, T3)
            generations = h.archive.all_generations(
                "association.example", "urn:association.example:n")
            self.assertEqual(len(generations), 1)
            entity = generations[0]
            self.assertEqual(entity.state, STATE_LIVE)
            self.assertEqual(len(entity.revisions), 2)
            self.assertEqual(len(entity.deletions), 1)  # 撤回历史仍在
        finally:
            h.close()

    def test_unsatisfied_evidence_still_splits(self):
        h = self._harness()
        try:
            h.run({
                "https://association.example/feed.atom": {
                    "http_status": 200, "body_inline": self.ENTRY_V1,
                    "etag": "v1"},
                "https://city.example/feed.atom": {"error": "down"},
                "https://wire.example/feed.atom": {"error": "down"},
            }, T1)
            h.run({
                "https://association.example/feed.atom": {
                    "http_status": 200,
                    "body_inline": (
                        '<feed xmlns="http://www.w3.org/2005/Atom" '
                        'xmlns:t="http://purl.org/atompub/tombstones/1.0">'
                        '<t:deleted-entry ref="urn:association.example:n" '
                        'when="2026-11-10T08:00:00Z" /></feed>'),
                    "etag": "v2"},
                "https://city.example/feed.atom": {"error": "down"},
                "https://wire.example/feed.atom": {"error": "down"},
            }, T2)
            # 标题和链接都变了：证据不满足，即便 continue 策略也必须分裂
            h.run({
                "https://association.example/feed.atom": {
                    "http_status": 200,
                    "body_inline": (
                        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
                        "<id>urn:association.example:n</id>"
                        "<title>完全不同的新通告</title>"
                        '<updated>2026-11-11T09:00:00Z</updated>'
                        '<link rel="alternate" href="https://association.example/other" />'
                        "</entry></feed>"),
                    "etag": "v3"},
                "https://city.example/feed.atom": {"error": "down"},
                "https://wire.example/feed.atom": {"error": "down"},
            }, T3)
            generations = h.archive.all_generations(
                "association.example", "urn:association.example:n")
            self.assertEqual(len(generations), 2)
            self.assertIn("证据不足", generations[1].split_reason)
        finally:
            h.close()


class ConfiguredEvidenceTests(unittest.TestCase):
    def test_configured_hint_confirms_relation_without_canonical(self):
        config_path = FIXT / "sources_hinted.json"
        h = Harness(config_path)
        try:
            h.run(script(1), T1)
            evidences = {r.evidence for r in h.archive.relations}
            self.assertIn("configured-evidence", evidences)
        finally:
            h.close()

    def test_hint_with_unaccepted_evidence_stays_candidate(self):
        config_path = FIXT / "sources_weak_hint.json"
        h = Harness(config_path)
        try:
            h.run(script(1), T1)
            self.assertEqual(
                [r for r in h.archive.relations
                 if r.evidence == "configured-evidence"],
                [],
            )
            self.assertTrue(
                any(c.reason == "configured-evidence"
                    for c in h.archive.candidates)
            )
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
