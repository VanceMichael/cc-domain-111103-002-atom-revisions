"""离线演示：跑通五轮合成 Atom 响应并打印归档视图。

仅读取 fixtures/offline 下的虚构材料，不访问任何真实站点。
归档与缓存写入 runtime/（已被 .gitignore 忽略）。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.collector import (
    Collector,
    OfflineFetcher,
    ResponseCache,
)
from app.config import load_config
from app.model import Archive

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures" / "offline"
RUNTIME = ROOT / "runtime"

ROUND_TIMES = {
    1: "2026-11-09T10:00:00Z",
    2: "2026-11-10T10:00:00Z",
    3: "2026-11-11T10:00:00Z",
    4: "2026-11-11T11:00:00Z",
    5: "2026-11-11T15:00:00Z",
}
CACHE_LOSS_BEFORE_ROUND = 4


def _line(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    config = load_config(FIXTURES / "sources.json")
    archive = Archive(RUNTIME / "archive")
    cache = ResponseCache(RUNTIME / "cache")

    for round_no in range(1, 6):
        script = json.loads((FIXTURES / f"round{round_no}" / "script.json").read_text(
            encoding="utf-8"))
        if round_no == CACHE_LOSS_BEFORE_ROUND:
            shutil.rmtree(cache.dir)  # 模拟本地缓存整体丢失
        fetcher = OfflineFetcher(script, base_dir=FIXTURES)
        collector = Collector(config, archive, cache, fetcher)
        run = collector.run(observed_at=ROUND_TIMES[round_no])
        _line(f"第 {round_no} 轮采集 ({run.observed_at})")
        for fs in run.feeds:
            print(f"  [{fs.status:>24}] {fs.feed_key:<11} {fs.detail}")

    _line("按发布者查看当前条目")
    for publisher in sorted({f.publisher_id for f in config.feeds}):
        print(f"发布者 {publisher}:")
        for entity in archive.entities_by_publisher(publisher):
            head = entity.latest
            print(
                f"  - {entity.entry_id} (gen {entity.generation}, "
                f"{entity.state}, 修订 {len(entity.revisions)} 次, "
                f"撤回 {len(entity.deletions)} 次)"
            )
            if head:
                print(f"      标题: {head.title}")
                if head.summary:
                    print(f"      摘要: {head.summary[:42]}...")
                if head.content:
                    print(f"      原文: 已补发 ({len(head.content)} 字)")
                for lk in head.links:
                    print(f"      链接[{lk.rel}]: {lk.href}")
            if entity.predecessor_uid:
                print(f"      身份分裂自: {entity.predecessor_uid}（{entity.split_reason}）")

    _line("历史修订链（协会 notice-2026-11）")
    for entity in archive.history("association.example",
                                  "urn:association.example:notice-2026-11"):
        for rev in entity.revisions:
            print(f"  rev{rev.seq} @ {rev.observed_at} "
                  f"declared-updated={rev.declared_updated} 标题={rev.title}")

    _line("已确认的转载关系与弱匹配候选")
    for rel in archive.relations:
        print(f"  确认: {rel.source_uid} -> {rel.target_uid} ({rel.evidence})")
    for cand in archive.candidates:
        print(f"  候选: {cand.source_uid} ~ {cand.target_uid or '∅'} "
              f"({cand.reason}) {cand.detail}")

    _line("时点重建：第 1 轮结束时已知内容")
    view = archive.view_at_run(1)
    for publisher in sorted({f.publisher_id for f in config.feeds}):
        for entity in view.entities_by_publisher(publisher):
            head = entity.latest
            print(f"  {publisher} {entity.entry_id}: {head.title if head else '（无正文）'}")
    print(f"  当时确认关系 {len(view.relations)} 条、候选 {len(view.candidates)} 条")
    print(f"  （重建视图实体数 {len(view.entities)}，当前视图实体数 {len(archive.entities)}）")

    _line("时点重建：第 4 轮（缓存丢失后）结束时已知内容")
    view4 = archive.view_at_run(4)
    notice10 = view4.get(
        "association.example", "urn:association.example:notice-2026-10")
    gens_in_view = [
        e for e in view4.entities.values()
        if e.publisher_id == "association.example"
        and e.entry_id == "urn:association.example:notice-2026-10"
    ]
    print(f"  notice-2026-10 在第 4 轮视图中的世代数: {len(gens_in_view)} "
          f"（当前世代状态: {notice10.state}）；当前归档已有 3 代，证明视图未混入第 5 轮")


if __name__ == "__main__":
    main()
