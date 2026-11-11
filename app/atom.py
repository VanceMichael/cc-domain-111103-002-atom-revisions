"""Atom 订阅源解析。

职责边界：只负责把离线 Atom 响应解析成结构化数据，保留
entry 在发布者命名空间下的身份、原始内容摘要、链接与发布者声明时间，
以及 RFC 6721 风格的 deleted-entry（tombstone）。
本模块不做跨 feed 合并、不推断删除、不访问网络。
"""
from __future__ import annotations

from dataclasses import dataclass
import xml.etree.ElementTree as ET

ATOM_NS = "http://www.w3.org/2005/Atom"
TOMBSTONE_LOCAL = "deleted-entry"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.strip()


@dataclass(frozen=True)
class Link:
    """Atom 链接，保留原始 rel/href；缺省 rel 按 Atom 规范视为 alternate。"""

    href: str
    rel: str = "alternate"
    type: str = ""
    title: str = ""


@dataclass(frozen=True)
class ParsedEntry:
    entry_id: str
    title: str = ""
    updated: str = ""        # 发布者声明的 updated
    published: str = ""      # 发布者声明的 published
    summary: str = ""        # atom:summary 原文摘要
    content: str = ""        # atom:content 原文（补发后的完整内容）
    links: tuple[Link, ...] = ()


@dataclass(frozen=True)
class ParsedTombstone:
    """deleted-entry：发布者显式声明的撤回。"""

    ref: str
    when: str = ""


@dataclass(frozen=True)
class ParsedFeed:
    entries: tuple[ParsedEntry, ...] = ()
    tombstones: tuple[ParsedTombstone, ...] = ()
    feed_id: str = ""
    feed_updated: str = ""


@dataclass(frozen=True)
class Entry:
    """旧版读取接口使用的精简结构，保持向后兼容。"""

    entry_id: str
    title: str
    updated: str


def parse_feed(raw: bytes | str) -> ParsedFeed:
    root = ET.fromstring(raw)
    entries: list[ParsedEntry] = []
    tombstones: list[ParsedTombstone] = []
    feed_id = ""
    feed_updated = ""
    for child in list(root):
        name = _local(child.tag)
        if name == "entry":
            entries.append(_parse_entry(child))
        elif name == TOMBSTONE_LOCAL:
            # 兼容默认命名空间与 RFC 6721 tombstone 命名空间两种写法
            ref = child.get("ref", "")
            if not ref:
                raise ValueError("deleted-entry 缺少 ref 属性")
            tombstones.append(ParsedTombstone(ref=ref, when=child.get("when", "")))
        elif name == "id":
            feed_id = _text(child)
        elif name == "updated":
            feed_updated = _text(child)
    return ParsedFeed(
        entries=tuple(entries),
        tombstones=tuple(tombstones),
        feed_id=feed_id,
        feed_updated=feed_updated,
    )


def _parse_entry(node: ET.Element) -> ParsedEntry:
    links: list[Link] = []
    fields = {k: "" for k in ("id", "title", "updated", "published", "summary", "content")}
    for child in list(node):
        name = _local(child.tag)
        if name == "link":
            links.append(
                Link(
                    href=child.get("href", ""),
                    rel=child.get("rel", "alternate"),
                    type=child.get("type", ""),
                    title=child.get("title", ""),
                )
            )
        elif name in fields:
            fields[name] = _text(child)
    if not fields["id"]:
        raise ValueError("Atom entry 缺少 id")
    return ParsedEntry(
        entry_id=fields["id"],
        title=fields["title"],
        updated=fields["updated"],
        published=fields["published"],
        summary=fields["summary"],
        content=fields["content"],
        links=tuple(links),
    )


def parse_entries(raw: bytes | str) -> list[Entry]:
    """旧接口：仅返回 entry 身份、标题与更新时间。"""
    return [Entry(e.entry_id, e.title, e.updated) for e in parse_feed(raw).entries]
