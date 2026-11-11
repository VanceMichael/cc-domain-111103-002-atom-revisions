import json
import unittest
from pathlib import Path
from app.atom import parse_entries
class DomainTests(unittest.TestCase):
    def test_namespaces(self): self.assertEqual(parse_entries(Path("fixtures/association.xml").read_bytes())[0].entry_id,"urn:association:notice-1")
    def test_multiple(self): self.assertEqual(len(parse_entries(b'<feed><entry><id>a</id></entry><entry><id>b</id></entry></feed>')),2)
    def test_malformed(self):
        with self.assertRaises(Exception): parse_entries(b"<feed>")

class FixtureTests(unittest.TestCase):
    def test_fixture_directory(self): self.assertTrue(Path("fixtures").is_dir())
