"""来源配置。

配置显式声明：
- 每个 feed 归属的发布者命名空间（entry 身份按命名空间隔离）；
- feed 地址迁移链（迁移后旧缓存/旧条目不会自动嫁接到新地址实体）；
- ID 重用时的身份策略（split 默认分裂新实体 / continue 需显式证据）；
- 转载关系可接受的证据强度，弱匹配只保留为候选。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

# ID 重用策略
REUSE_SPLIT = "split"        # 默认：新旧同 ID 视为不同实体，旧记录不嫁接
REUSE_CONTINUE = "continue"  # 需配置连续性证据才接续到同一实体

# 转载证据强度（由强到弱）
EVIDENCE_CANONICAL = "canonical-link"
EVIDENCE_CONFIGURED = "configured-evidence"
EVIDENCE_WEAK_TITLE = "weak-title-match"


@dataclass(frozen=True)
class FeedLocation:
    key: str
    publisher_id: str
    # 当前地址排在第一个；历史地址按迁移先后排列。
    # 地址变化本身不改变实体身份，身份只由 publisher_id + entry_id 决定。
    urls: tuple[str, ...]
    migrated_from: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReusePolicy:
    mode: str = REUSE_SPLIT
    # continue 模式下要求同时满足的连续性证据，例如：
    # ["declared-updated-monotonic", "link-href-persists"]
    continuity_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceConfig:
    feeds: tuple[FeedLocation, ...]
    reuse: ReusePolicy = field(default_factory=ReusePolicy)
    # 达到该强度的证据才建立确认关系；其余进入候选
    min_relation_evidence: str = EVIDENCE_CANONICAL
    # 哪些强度被视为可配置的强证据（除 canonical 外）
    accepted_evidence: tuple[str, ...] = (EVIDENCE_CANONICAL,)
    # 显式登记的转载对应关系（configured-evidence）
    relation_hints: tuple[dict, ...] = ()

    def feed(self, key: str) -> FeedLocation:
        for loc in self.feeds:
            if loc.key == key:
                return loc
        raise KeyError(f"未知 feed: {key}")

    def publisher_feeds(self) -> dict[str, list[FeedLocation]]:
        grouped: dict[str, list[FeedLocation]] = {}
        for loc in self.feeds:
            grouped.setdefault(loc.publisher_id, []).append(loc)
        return grouped

    def current_url(self, key: str) -> str:
        return self.feed(key).urls[0]

    def is_known_url(self, url: str) -> bool:
        return any(url in loc.urls for loc in self.feeds)

    def feed_for_url(self, url: str) -> FeedLocation | None:
        for loc in self.feeds:
            if url in loc.urls:
                return loc
        return None

    def evidence_accepted(self, evidence: str) -> bool:
        return evidence in self.accepted_evidence


def load_config(path: str | Path) -> SourceConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    feeds = tuple(
        FeedLocation(
            key=item["key"],
            publisher_id=item["publisher_id"],
            urls=tuple(item["urls"]),
            migrated_from=tuple(item.get("migrated_from", [])),
        )
        for item in data.get("feeds", [])
    )
    reuse_data = data.get("id_reuse", {})
    reuse = ReusePolicy(
        mode=reuse_data.get("mode", REUSE_SPLIT),
        continuity_evidence=tuple(reuse_data.get("continuity_evidence", [])),
    )
    relation = data.get("relations", {})
    return SourceConfig(
        feeds=feeds,
        reuse=reuse,
        min_relation_evidence=relation.get(
            "min_evidence", EVIDENCE_CANONICAL
        ),
        accepted_evidence=tuple(
            relation.get(
                "accepted_evidence", [EVIDENCE_CANONICAL]
            )
        ),
        relation_hints=tuple(relation.get("hints", [])),
    )
