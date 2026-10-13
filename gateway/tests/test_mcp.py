import os
import sys
import tempfile
import unittest

from pac_gateway.mcp import ConnectorHub


class Notes(unittest.TestCase):
    def test_notes_connector_through_the_hub(self):
        with tempfile.TemporaryDirectory() as d:
            hub = ConnectorHub({"notes": {"command": [sys.executable, "-m", "pac_gateway.connectors.notes"],
                                          "env": {"NOTES_FILE": os.path.join(d, "n.json")}}})
            try:
                names = [t["name"] for t in hub.tools()]
                self.assertEqual(names, ["notes__add_note", "notes__list_notes"])
                self.assertIn("Saved note 1", hub.call_tool("notes__add_note", {"text": "明天开会"}))
                self.assertIn("明天开会", hub.call_tool("notes__list_notes", {}))
            finally:
                hub.close()


if __name__ == "__main__":
    unittest.main()
