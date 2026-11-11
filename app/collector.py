"""采集编排：抓取、缓存、摄入、关系判定。

关键安全语义：
1. 只有发布者在响应中显式给出的 deleted-entry 才会产生 tombstone；
   feed 里“没出现”的旧条目永远不会被推断为删除。
2. 单个 feed 传输失败或解析失败时，仅标记该 feed 失败并跳过，
   既不撤回它的旧条目，也不影响同一轮内其他已完整取得的 feed。
3. 304 必须能对应到本地缓存版本才算“内容未变”；本地没有可对应
   缓存时单独标记，按不可用处理而不是复用未知内容。
4. entry 身份绑定发布者命名空间，feed 地址迁移不改变身份；
   ID 重用默认分裂为新一代实体，旧记录不嫁接到新实体。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Protocol

from app.atom import Link, ParsedFeed, parse_feed
from app.config import (
    EVIDENCE_CANONICAL,
    EVIDENCE_CONFIGURED,
    EVIDENCE_WEAK_TITLE,
    REUSE_CONTINUE,
    SourceConfig,
)
from app.model import (
    Archive,
    ArchiveView,
    Candidate,
    DeletionRecord,
    Entity,
    FeedStatus,
    LinkRecord,
    Relation,
    Revision,
    RunRecord,
    STATE_DELETED,
    STATE_LIVE,
    content_hash,
    make_uid,
)

# Feed 采集状态
ST_OK = "ok"
ST_NOT_MODIFIED = "not_modified"
ST_NOT_MODIFIED_NO_CACHE = "not_modified_no_cache"
ST_FAILED = "failed"


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# 抓取抽象（离线实现，不访问网络）
# --------------------------------------------------------------------------- #


@dataclass
class HttpResponse:
    url: str
    status: int                      # HTTP 状态码；0 表示传输层故障
    body: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str = ""


class Fetcher(Protocol):
    def fetch(self, url: str, etag: str | None, last_modified: str | None) -> HttpResponse: ...


class OfflineFetcher:
    """按脚本返回离线响应。

    脚本是 url -> 响应规格的映射，规格示例::

        {"http_status": 200, "body_file": "round1/association.xml",
         "etag": "v1", "last_modified": "Mon, 10 Nov 2026 09:00:00 GMT"}
        {"http_status": 304, "etag": "v2"}
        {"error": "connection refused"}
    """

    def __init__(self, script: dict, base_dir: str | Path | None = None):
        self._script = script
        self._base = Path(base_dir) if base_dir else None

    def fetch(self, url: str, etag: str | None, last_modified: str | None) -> HttpResponse:
        spec = self._script.get(url)
        if spec is None:
            return HttpResponse(url=url, status=0, error=f"离线脚本缺少该地址的响应: {url}")
        if spec.get("error"):
            return HttpResponse(url=url, status=0, error=spec["error"])
        body = None
        if "body_file" in spec:
            path = Path(spec["body_file"])
            if self._base is not None and not path.is_absolute():
                path = self._base / path
            body = path.read_bytes()
        elif "body_inline" in spec:
            body = spec["body_inline"].encode("utf-8")
        return HttpResponse(
            url=url,
            status=int(spec.get("http_status", 200)),
            body=body,
            etag=spec.get("etag"),
            last_modified=spec.get("last_modified"),
        )


# --------------------------------------------------------------------------- #
# 条件请求缓存（与归档分离，可单独丢失）
# --------------------------------------------------------------------------- #


@dataclass
class CachedResponse:
    etag: str | None
    last_modified: str | None
    body: bytes


class ResponseCache:
    """每个 feed 保存最近一次 200 响应及其校验器。"""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, feed_key: str) -> Path:
        return self.dir / f"{feed_key}.json"

    def get(self, feed_key: str) -> CachedResponse | None:
        path = self._path(feed_key)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return CachedResponse(
            etag=data.get("etag"),
            last_modified=data.get("last_modified"),
            body=data["body"].encode("utf-8"),
        )

    def store(self, feed_key: str, resp: HttpResponse) -> None:
        # 目录可能因“缓存丢失”演练被整体删除，写入前确保存在
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(feed_key).write_text(
            json.dumps(
                {
                    "etag": resp.etag,
                    "last_modified": resp.last_modified,
                    "body": (resp.body or b"").decode("utf-8"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def discard(self, feed_key: str) -> None:
        """模拟缓存丢失。"""
        path = self._path(feed_key)
        if path.exists():
            path.unlink()


# --------------------------------------------------------------------------- #
# 摄入：把一份成功解析的 feed 写入归档
# --------------------------------------------------------------------------- #


class Ingester:
    def __init__(self, archive: Archive, config: SourceConfig):
        self.archive = archive
        self.config = config

    def ingest(self, feed_key: str, publisher_id: str, parsed: ParsedFeed,
               run_id: int, observed_at: str) -> None:
        # 先处理 tombstone 再处理 entry：同一响应里“撤回旧条目并重用其
        # ID 发布新内容”时，旧实体先进入 deleted，新内容走 ID 重用判定，
        # 而不是被当作同一实体的普通修订。
        for tomb in parsed.tombstones:
            self._ingest_tombstone(publisher_id, tomb.ref, tomb.when,
                                   run_id, observed_at)
        for entry in parsed.entries:
            self._ingest_entry(publisher_id, entry, run_id, observed_at)

    def _ingest_entry(self, publisher_id: str, entry, run_id: int,
                      observed_at: str) -> None:
        digest = content_hash(entry.title, entry.summary, entry.content)
        current = self.archive.get(publisher_id, entry.entry_id)

        if current is None:
            entity = self._new_entity(publisher_id, entry.entry_id, observed_at)
            self._add_revision(entity, entry, digest, run_id, observed_at)
            return

        if current.state == STATE_LIVE:
            latest = current.latest
            if latest is not None and latest.content_hash == digest:
                return  # 内容未变，不产生空修订
            self._add_revision(current, entry, digest, run_id, observed_at)
            return

        # 当前实体处于 deleted：这是发布者对 entry ID 的重用
        self._handle_reused_id(current, entry, digest, run_id, observed_at)

    def _handle_reused_id(self, predecessor: Entity, entry, digest: str,
                          run_id: int, observed_at: str) -> None:
        """deleted 状态下同 ID 再次出现 entry：可能是撤回后恢复，也可能是
        ID 被重用于新实体。默认保守分裂，只有显式连续性证据齐备才接续。"""
        policy = self.config.reuse
        if policy.mode == REUSE_CONTINUE and self._continuity_holds(
            predecessor, entry, policy.continuity_evidence
        ):
            # 显式证据成立：恢复同一实体，完整保留“发布→撤回→恢复”历史链
            self.archive.record_revival(
                predecessor, run_id, observed_at,
                note="连续性证据满足: " + ",".join(policy.continuity_evidence),
            )
            self._add_revision(predecessor, entry, digest, run_id, observed_at)
            return

        if policy.mode == REUSE_CONTINUE:
            missing = [
                name for name in policy.continuity_evidence
                if not self._evidence_holds(predecessor, entry, name)
            ]
            reason = "ID 重用且连续性证据不足: " + ",".join(missing)
        else:
            reason = "ID 重用默认分裂策略：旧记录不嫁接到新实体"
        new_entity = Entity(
            uid=make_uid(predecessor.publisher_id, predecessor.entry_id,
                         predecessor.generation + 1),
            publisher_id=predecessor.publisher_id,
            entry_id=predecessor.entry_id,
            generation=predecessor.generation + 1,
            state=STATE_LIVE,
            predecessor_uid=predecessor.uid,
            split_reason=reason,
            created_at=observed_at,
        )
        self.archive.entities[new_entity.uid] = new_entity
        self.archive.record_split(
            new_entity, predecessor.uid, run_id, observed_at, reason
        )
        self._add_revision(new_entity, entry, digest, run_id, observed_at)

    @staticmethod
    def _evidence_holds(predecessor: Entity, entry, name: str) -> bool:
        latest = predecessor.latest
        if name == "entry-id-persists":
            return True
        if name == "title-persists":
            return latest is not None and latest.title == entry.title
        if name == "summary-persists":
            return latest is not None and bool(latest.summary) and (
                latest.summary == entry.summary or latest.title == entry.title
            )
        if name == "declared-updated-monotonic":
            return (
                latest is not None
                and bool(entry.updated)
                and entry.updated >= latest.declared_updated
            )
        if name == "link-href-persists":
            old = set(predecessor.link_hrefs()) if latest else set()
            new = {lk.href for lk in entry.links if lk.href}
            return bool(old & new)
        raise ValueError(f"未知连续性证据: {name}")

    def _continuity_holds(self, predecessor: Entity, entry, evidence) -> bool:
        return all(self._evidence_holds(predecessor, entry, n) for n in evidence)

    def _ingest_tombstone(self, publisher_id: str, entry_id: str,
                          declared_when: str, run_id: int,
                          observed_at: str) -> None:
        current = self.archive.get(publisher_id, entry_id)
        if current is None:
            # 首次见到的身份就是 tombstone：保留为“仅撤回记录”实体
            entity = self._new_entity(publisher_id, entry_id, observed_at,
                                      state=STATE_DELETED)
        else:
            entity = current
        self.archive.record_tombstone(
            entity,
            DeletionRecord(observed_at=observed_at, declared_when=declared_when),
            run_id,
        )

    def _new_entity(self, publisher_id: str, entry_id: str,
                    observed_at: str, state: str = STATE_LIVE) -> Entity:
        entity = Entity(
            uid=make_uid(publisher_id, entry_id),
            publisher_id=publisher_id,
            entry_id=entry_id,
            state=state,
            created_at=observed_at,
        )
        self.archive.entities[entity.uid] = entity
        return entity

    def _add_revision(self, entity: Entity, entry, digest: str,
                      run_id: int, observed_at: str) -> None:
        revision = Revision(
            seq=len(entity.revisions) + 1,
            observed_at=observed_at,
            declared_updated=entry.updated,
            declared_published=entry.published,
            title=entry.title,
            summary=entry.summary,
            content=entry.content,
            links=[self._link_record(lk) for lk in entry.links],
            content_hash=digest,
        )
        self.archive.record_revision(entity, revision, run_id, observed_at)

    @staticmethod
    def _link_record(link: Link) -> LinkRecord:
        return LinkRecord(href=link.href, rel=link.rel,
                          type=link.type, title=link.title)


# --------------------------------------------------------------------------- #
# 转载关系引擎
# --------------------------------------------------------------------------- #


class RelationEngine:
    """只基于显式 canonical 链接或配置证据确认关系；弱匹配留候选。"""

    def __init__(self, archive: Archive, config: SourceConfig, run_id: int = 0):
        self.archive = archive
        self.config = config
        self.run_id = run_id
        self._seen_relations: set[tuple[str, str, str]] = {
            (r.source_uid, r.target_uid, r.evidence)
            for r in archive.relations
        }
        self._seen_candidates: set[tuple[str, str, str]] = {
            (c.source_uid, c.target_uid, c.reason)
            for c in archive.candidates
        }

    def evaluate(self, run_id: int, observed_at: str) -> tuple[int, int]:
        self.run_id = run_id
        entities = list(self.archive.entities.values())
        href_index = self._build_href_index(entities)
        confirmed_pairs: set[tuple[str, str]] = set()

        for source in entities:
            for href in source.link_hrefs(rel="canonical"):
                target = href_index.get(href)
                if target is None or target.uid == source.uid:
                    if target is None:
                        self._candidate(
                            source, None, "canonical-target-unresolved",
                            f"canonical 链接无对应实体: {href}", observed_at
                        )
                    continue
                confirmed_pairs.add((source.uid, target.uid))
                self._relation(source, target, EVIDENCE_CANONICAL,
                               f"canonical: {href}", observed_at)

        # 配置证据（显式登记的转载对应关系）
        for hint in getattr(self.config, "relation_hints", ()):
            source = self.archive.get(hint["source_publisher"], hint["source_entry_id"])
            target = self.archive.get(hint["target_publisher"], hint["target_entry_id"])
            if source is None or target is None:
                continue
            evidence = hint.get("evidence", EVIDENCE_CONFIGURED)
            if self.config.evidence_accepted(evidence):
                confirmed_pairs.add((source.uid, target.uid))
                self._relation(source, target, evidence,
                               hint.get("detail", "配置证据"), observed_at)
            else:
                self._candidate(source, target, evidence,
                                hint.get("detail", "证据强度不足"), observed_at)

        self._weak_title_matches(entities, confirmed_pairs, observed_at)
        return (
            sum(1 for r in self.archive.relations if r.run_id == run_id),
            sum(1 for c in self.archive.candidates if c.run_id == run_id),
        )

    def _build_href_index(self, entities: list[Entity]) -> dict[str, Entity]:
        """实体链接索引。

        canonical 链接是“指向别处”的证据，不能作为实体自身的身份链接，
        否则转载方会把原作者的 URL 抢先登记到自己名下。这里只索引
        非 canonical（alternate 等）链接。
        """
        index: dict[str, Entity] = {}
        for entity in entities:
            if entity.state != STATE_LIVE or entity.latest is None:
                continue
            for link in entity.latest.links:
                if link.rel == "canonical":
                    continue
                index.setdefault(link.href, entity)
        return index

    def _relation(self, source: Entity, target: Entity, evidence: str,
                  detail: str, observed_at: str) -> None:
        key = (source.uid, target.uid, evidence)
        if key in self._seen_relations:
            return
        self._seen_relations.add(key)
        self.archive.record_relation(
            Relation(
                source_uid=source.uid,
                target_uid=target.uid,
                evidence=evidence,
                detail=detail,
                observed_at=observed_at,
                run_id=self.run_id,
            )
        )

    def _candidate(self, source: Entity, target: Entity | None, reason: str,
                   detail: str, observed_at: str) -> None:
        target_uid = target.uid if target else ""
        key = (source.uid, target_uid, reason)
        if key in self._seen_candidates:
            return
        self._seen_candidates.add(key)
        self.archive.record_candidate(
            Candidate(
                source_uid=source.uid,
                target_uid=target_uid,
                reason=reason,
                detail=detail,
                observed_at=observed_at,
                run_id=self.run_id,
            )
        )

    def _weak_title_matches(self, entities: list[Entity],
                            confirmed_pairs: set[tuple[str, str]],
                            observed_at: str) -> None:
        live = [e for e in entities if e.state == STATE_LIVE and e.latest]
        seen_pairs: set[tuple[str, str]] = set()
        for i, source in enumerate(live):
            for target in live[i + 1:]:
                if source.publisher_id == target.publisher_id:
                    continue
                pair = (source.uid, target.uid)
                reverse = (target.uid, source.uid)
                if pair in confirmed_pairs or reverse in confirmed_pairs:
                    continue
                if pair in seen_pairs or reverse in seen_pairs:
                    continue
                if source.latest.title and source.latest.title == target.latest.title:
                    seen_pairs.add(pair)
                    self._candidate(
                        source, target, EVIDENCE_WEAK_TITLE,
                        f"标题相同但无显式证据: {source.latest.title}",
                        observed_at,
                    )


# --------------------------------------------------------------------------- #
# 采集编排
# --------------------------------------------------------------------------- #


class Collector:
    def __init__(self, config: SourceConfig, archive: Archive,
                 cache: ResponseCache, fetcher: Fetcher):
        self.config = config
        self.archive = archive
        self.cache = cache
        self.fetcher = fetcher
        self.ingester = Ingester(archive, config)

    def run(self, observed_at: str | None = None) -> RunRecord:
        observed_at = observed_at or utc_now_iso()
        run_id = self.archive.next_run_id()
        record = RunRecord(run_id=run_id, observed_at=observed_at)

        for loc in self.config.feeds:
            record.feeds.append(
                self._collect_one(loc.key, loc.publisher_id,
                                  loc.urls[0], run_id, observed_at)
            )

        rel_count, cand_count = RelationEngine(
            self.archive, self.config
        ).evaluate(run_id, observed_at)
        record.relation_count = rel_count
        record.candidate_count = cand_count
        self.archive.record_run(record)
        return record

    def _collect_one(self, feed_key: str, publisher_id: str, url: str,
                     run_id: int, observed_at: str) -> FeedStatus:
        cached = self.cache.get(feed_key)
        resp = self.fetcher.fetch(
            url,
            cached.etag if cached else None,
            cached.last_modified if cached else None,
        )

        if resp.status == 0:
            # 传输层故障：旧条目原样保留，不产生任何删除推断
            return FeedStatus(feed_key, publisher_id, url, ST_FAILED,
                              observed_at, detail=resp.error)

        if resp.status == 304:
            if cached is None or not self._validator_matches(cached, resp):
                # 304 但本地没有可对应的缓存版本：无法确认内容，按不可用处理
                return FeedStatus(
                    feed_key, publisher_id, url,
                    ST_NOT_MODIFIED_NO_CACHE, observed_at,
                    detail="收到 304 但本地无可对应缓存版本，未复用任何内容",
                    etag=resp.etag, last_modified=resp.last_modified,
                )
            return FeedStatus(feed_key, publisher_id, url, ST_NOT_MODIFIED,
                              observed_at, detail="条件请求确认内容未变",
                              etag=cached.etag,
                              last_modified=cached.last_modified)

        if resp.status != 200 or resp.body is None:
            return FeedStatus(feed_key, publisher_id, url, ST_FAILED,
                              observed_at,
                              detail=f"未预期的 HTTP 状态: {resp.status}")

        try:
            parsed = parse_feed(resp.body)
        except Exception as exc:  # 解析失败同样隔离，旧条目不受影响
            return FeedStatus(feed_key, publisher_id, url, ST_FAILED,
                              observed_at, detail=f"解析失败: {exc}")

        self.ingester.ingest(feed_key, publisher_id, parsed,
                             run_id, observed_at)
        self.cache.store(feed_key, resp)
        return FeedStatus(feed_key, publisher_id, url, ST_OK, observed_at,
                          detail=f"{len(parsed.entries)} entries / "
                                 f"{len(parsed.tombstones)} tombstones",
                          etag=resp.etag, last_modified=resp.last_modified)

    @staticmethod
    def _validator_matches(cached: CachedResponse, resp: HttpResponse) -> bool:
        if resp.etag is not None:
            return cached.etag == resp.etag
        if resp.last_modified is not None:
            return cached.last_modified == resp.last_modified
        # 304 未带任何校验器，无法证明与本地版本对应
        return False

    def view_at_run(self, run_id: int) -> ArchiveView:
        return self.archive.view_at_run(run_id)

    def view_at_time(self, observed_at: str) -> ArchiveView:
        return self.archive.view_at_time(observed_at)
