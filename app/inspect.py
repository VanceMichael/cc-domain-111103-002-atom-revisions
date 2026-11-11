from pathlib import Path
from app.atom import parse_entries
print(parse_entries(Path("fixtures/association.xml").read_bytes()))
