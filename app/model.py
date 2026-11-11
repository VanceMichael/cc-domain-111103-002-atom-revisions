"""归档领域模型与追加式存储。

身份规则：
- entry 身份是 (publisher_id, entry_id) 的组合，不同 feed 的相同 ID
  互不属于同一实体，绝不会互相覆盖。
- 归档只追加事件（revision / tombstone / revival），所有当前状态与
  历史时点视图都由事件重放得到，因此完整修订链和撤回记录不会丢失。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import hashlib
import json
from pathlib import Path

# 实体 UID 中各段的分隔符；entry_id 若含该字符会被原样保留，
# 因为所有解析都按已知段数切分，不会产生歧义。
UID_SEP = "!"
GEN_SEP = "#"

# 实体生命周期状态
STATE_LIVE = "live"
STATE_DELETED = "deleted"


def content_hash(title: str, summary: str, content: str) -> str:
    """内容指纹：用于判断一次采集是否带来实际修订。"""
    payload = json.dumps(
        {"title": title, "summary": summary, "content": content},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def make_uid(publisher_id: str, entry_id: str, generation: int = 1) -> str:
    return f"{publisher_id}{UID_SEP}{entry_id}{GEN_SEP}{generation}"


def parse_uid(uid: str) -> tuple[str, str, int]:
    head, _, gen = uid.rpartition(GEN_SEP)
    publisher, _, entry_id = head.partition(UID_SEP)
    return publisher, entry_id, int(gen)


@dataclass
class LinkRecord:
    href: str
    rel: str = "alternate"
    type: str = ""
    title: str = ""


@dataclass
class Revision:
    """一次发布者声明的内容版本（entry 身份不变）。"""

    seq: int
    observed_at: str
    declared_updated: str
    declared_published: str
    title: str
    summary: str
    content: str
    links: list[LinkRecord] = field(default_factory=list)
    content_hash: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DeletionRecord:
    """一次 tombstone 声明；同一条目可被撤回后再恢复、再撤回。"""

    observed_at: str
    declared_when: str


@dataclass
class Entity:
    """发布者命名空间下的一个条目实体及其完整历史。"""

    uid: str
    publisher_id: str
    entry_id: str
    generation: int = 1
    state: str = STATE_LIVE
    revisions: list[Revision] = field(default_factory=list)
    deletions: list[DeletionRecord] = field(default_factory=list)
    # 与同身份前序实体的接续关系（ID 重用 continue 策略时写入）
    predecessor_uid: str | None = None
    # 身份分裂的原因证据，便于人工审计为何没有嫁接到旧记录
    split_reason: str = ""
    created_at: str = ""

    @property
    def latest(self) -> Revision | None:
        return self.revisions[-1] if self.revisions else None

    @property
    def is_deleted(self) -> bool:
        return self.state == STATE_DELETED

    def link_hrefs(self, rel: str | None = None) -> list[str]:
        if self.latest is None:
            return []
        return [lk.href for lk in self.latest.links if rel is None or lk.rel == rel]


@dataclass
class Relation:
    """已确认的转载/指向关系，证据强度必须达到配置阈值。"""

    source_uid: str
    target_uid: str
    evidence: str          # 例如 canonical-link / configured-evidence
    detail: str = ""
    observed_at: str = ""
    run_id: int = 0


@dataclass
class Candidate:
    """弱匹配候选：仅保留供人工核对，不做合并。"""

    source_uid: str
    target_uid: str
    reason: str
    detail: str
    observed_at: str
    run_id: int = 0


@dataclass
class FeedStatus:
    feed_key: str
    publisher_id: str
    url: str
    status: str            # ok / not_modified / failed / not_modified_no_cache
    observed_at: str
    detail: str = ""
    etag: str | None = None
    last_modified: str | None = None


@dataclass
class RunRecord:
    run_id: int
    observed_at: str
    feeds: list[FeedStatus] = field(default_factory=list)
    relation_count: int = 0
    candidate_count: int = 0


# --------------------------------------------------------------------------- #
# 追加式事件归档
# --------------------------------------------------------------------------- #

EV_REVISION = "revision"
EV_TOMBSTONE = "tombstone"
EV_REVIVAL = "revival"
EV_RELATION = "relation"
EV_CANDIDATE = "candidate"
EV_FEED = "feed"
EV_RUN = "run"
EV_SPLIT = "split"


class Archive:
    """JSONL 事件日志 + 内存投影。

    目录布局::

        archive_dir/
          events.jsonl        全部不可变事件
          runs.json           采集轮次索引
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events_path = self.directory / "events.jsonl"
        self.runs_path = self.directory / "runs.json"
        self.entities: dict[str, Entity] = {}
        self.relations: list[Relation] = []
        self.candidates: list[Candidate] = []
        self.runs: list[RunRecord] = []
        self._replay()

    # ---------------- 事件追加 ---------------- #

    def append_event(self, kind: str, payload: dict) -> None:
        record = {"kind": kind, **payload}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def record_revision(
        self,
        entity: Entity,
        revision: Revision,
        run_id: int,
        observed_at: str,
    ) -> None:
        entity.revisions.append(revision)
        entity.state = STATE_LIVE
        if not entity.created_at:
            entity.created_at = observed_at
        self.append_event(
            EV_REVISION,
            {
                "run_id": run_id,
                "observed_at": observed_at,
                "uid": entity.uid,
                "publisher_id": entity.publisher_id,
                "entry_id": entity.entry_id,
                "generation": entity.generation,
                "revision": revision.to_dict(),
            },
        )

    def record_tombstone(
        self,
        entity: Entity,
        deletion: DeletionRecord,
        run_id: int,
    ) -> None:
        entity.deletions.append(deletion)
        entity.state = STATE_DELETED
        self.append_event(
            EV_TOMBSTONE,
            {
                "run_id": run_id,
                "observed_at": deletion.observed_at,
                "uid": entity.uid,
                "publisher_id": entity.publisher_id,
                "entry_id": entity.entry_id,
                "declared_when": deletion.declared_when,
            },
        )

    def record_revival(
        self,
        entity: Entity,
        run_id: int,
        observed_at: str,
        note: str,
    ) -> None:
        entity.state = STATE_LIVE
        self.append_event(
            EV_REVIVAL,
            {
                "run_id": run_id,
                "observed_at": observed_at,
                "uid": entity.uid,
                "note": note,
            },
        )

    def record_split(
        self,
        new_entity: Entity,
        predecessor_uid: str,
        run_id: int,
        observed_at: str,
        reason: str,
    ) -> None:
        self.append_event(
            EV_SPLIT,
            {
                "run_id": run_id,
                "observed_at": observed_at,
                "uid": new_entity.uid,
                "publisher_id": new_entity.publisher_id,
                "entry_id": new_entity.entry_id,
                "generation": new_entity.generation,
                "predecessor_uid": predecessor_uid,
                "reason": reason,
            },
        )

    def record_relation(self, relation: Relation) -> None:
        self.relations.append(relation)
        self.append_event(EV_RELATION, asdict(relation))

    def record_candidate(self, candidate: Candidate) -> None:
        self.candidates.append(candidate)
        self.append_event(EV_CANDIDATE, asdict(candidate))

    def record_run(self, run: RunRecord) -> None:
        self.runs.append(run)
        self.append_event(
            EV_RUN,
            {
                "run_id": run.run_id,
                "observed_at": run.observed_at,
                "relation_count": run.relation_count,
                "candidate_count": run.candidate_count,
            },
        )
        for fs in run.feeds:
            self.append_event(EV_FEED, asdict(fs) | {"run_id": run.run_id})
        self.runs_path.write_text(
            json.dumps([asdict(r) for r in self.runs], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------------- 查询 ---------------- #

    def get(self, publisher_id: str, entry_id: str) -> Entity | None:
        """返回该身份当前世代的实体（世代链最新一代）。"""
        matches = [
            e
            for e in self.entities.values()
            if e.publisher_id == publisher_id and e.entry_id == entry_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda e: e.generation)

    def all_generations(self, publisher_id: str, entry_id: str) -> list[Entity]:
        return sorted(
            (
                e
                for e in self.entities.values()
                if e.publisher_id == publisher_id and e.entry_id == entry_id
            ),
            key=lambda e: e.generation,
        )

    def entities_by_publisher(self, publisher_id: str) -> list[Entity]:
        return sorted(
            (e for e in self.entities.values() if e.publisher_id == publisher_id),
            key=lambda e: e.uid,
        )

    def history(self, publisher_id: str, entry_id: str) -> list[Entity]:
        """该身份的全部世代与修订/tombstone 历史。"""
        return self.all_generations(publisher_id, entry_id)

    def next_run_id(self) -> int:
        return len(self.runs) + 1

    # ---------------- 时点重建 ---------------- #

    def view_at_run(self, run_id: int) -> "ArchiveView":
        """重放至第 run_id 轮采集结束时已知的全部内容。"""
        known = {r.run_id for r in self.runs}
        if known and run_id not in known:
            raise KeyError(
                f"未知采集轮次: {run_id}；已知轮次: {sorted(known)}"
            )
        return self._replay_view(lambda ev: ev.get("run_id", 0) <= run_id)

    def view_at_time(self, observed_at: str) -> "ArchiveView":
        """重放至某个观测时间点（ISO 字符串字典序可比，统一 UTC Z 格式）。"""
        return self._replay_view(lambda ev: ev.get("observed_at", "") <= observed_at)

    def _replay_view(self, predicate) -> "ArchiveView":
        view = ArchiveView()
        for raw in self._iter_event_lines():
            ev = json.loads(raw)
            if not predicate(ev):
                continue
            view.apply(ev)
        return view

    def _replay(self) -> None:
        view = ArchiveView()
        for raw in self._iter_event_lines():
            view.apply(json.loads(raw))
        self.entities = view.entities
        self.relations = view.relations
        self.candidates = view.candidates
        # run 索引从 runs.json 恢复；events 中的 run/feed 仅作审计日志
        if self.runs_path.exists():
            for data in json.loads(self.runs_path.read_text(encoding="utf-8")):
                self.runs.append(
                    RunRecord(
                        run_id=data["run_id"],
                        observed_at=data["observed_at"],
                        feeds=[FeedStatus(**f) for f in data.get("feeds", [])],
                        relation_count=data.get("relation_count", 0),
                        candidate_count=data.get("candidate_count", 0),
                    )
                )

    def _iter_event_lines(self):
        if self.events_path.exists():
            with self.events_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield line


class ArchiveView:
    """事件重放得到的只读投影，可表达任意历史时点的已知状态。"""

    def __init__(self):
        self.entities: dict[str, Entity] = {}
        self.relations: list[Relation] = []
        self.candidates: list[Candidate] = []

    def apply(self, ev: dict) -> None:
        kind = ev["kind"]
        if kind == EV_REVISION:
            entity = self._ensure_entity(ev)
            data = ev["revision"]
            entity.revisions.append(
                Revision(
                    seq=data["seq"],
                    observed_at=data["observed_at"],
                    declared_updated=data["declared_updated"],
                    declared_published=data.get("declared_published", ""),
                    title=data["title"],
                    summary=data["summary"],
                    content=data["content"],
                    links=[LinkRecord(**lk) for lk in data.get("links", [])],
                    content_hash=data.get("content_hash", ""),
                )
            )
            entity.state = STATE_LIVE
        elif kind == EV_TOMBSTONE:
            entity = self._ensure_entity(ev)
            entity.deletions.append(
                DeletionRecord(
                    observed_at=ev["observed_at"],
                    declared_when=ev.get("declared_when", ""),
                )
            )
            entity.state = STATE_DELETED
        elif kind == EV_REVIVAL:
            entity = self.entities.get(ev["uid"])
            if entity is not None:
                entity.state = STATE_LIVE
        elif kind == EV_SPLIT:
            new_entity = self._ensure_entity(ev)
            new_entity.predecessor_uid = ev["predecessor_uid"]
            new_entity.split_reason = ev["reason"]
        elif kind == EV_RELATION:
            self.relations.append(Relation(**{k: v for k, v in ev.items() if k != "kind"}))
        elif kind == EV_CANDIDATE:
            self.candidates.append(Candidate(**{k: v for k, v in ev.items() if k != "kind"}))

    def _ensure_entity(self, ev: dict) -> Entity:
        uid = ev["uid"]
        entity = self.entities.get(uid)
        if entity is None:
            entity = Entity(
                uid=uid,
                publisher_id=ev["publisher_id"],
                entry_id=ev["entry_id"],
                generation=ev.get("generation", 1),
                created_at=ev.get("observed_at", ""),
            )
            self.entities[uid] = entity
        return entity

    def get(self, publisher_id: str, entry_id: str) -> Entity | None:
        matches = [
            e
            for e in self.entities.values()
            if e.publisher_id == publisher_id and e.entry_id == entry_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda e: e.generation)

    def entities_by_publisher(self, publisher_id: str) -> list[Entity]:
        return sorted(
            (e for e in self.entities.values() if e.publisher_id == publisher_id),
            key=lambda e: e.uid,
        )
