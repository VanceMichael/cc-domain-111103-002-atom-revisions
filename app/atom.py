from dataclasses import dataclass
import xml.etree.ElementTree as ET

@dataclass(frozen=True)
class Entry:
    entry_id: str
    title: str
    updated: str

def parse_entries(raw):
    root=ET.fromstring(raw)
    result=[]
    for node in root.iter():
        if node.tag.rsplit("}",1)[-1]!="entry": continue
        fields={child.tag.rsplit("}",1)[-1]:child.text or "" for child in node}
        if not fields.get("id"): raise ValueError("Atom entry 缺少 id")
        result.append(Entry(fields["id"],fields.get("title",""),fields.get("updated","")))
    return result
