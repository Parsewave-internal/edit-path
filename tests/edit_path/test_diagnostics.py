import json, tempfile, unittest
from pathlib import Path
from edit_path.diagnostics import log_event
from edit_path.support_bundle import create_bundle

class DiagnosticsTests(unittest.TestCase):
    def test_logs_redact_secrets_and_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); log_event(root, "test", api_key="secret", detail="ok")
            line=json.loads((root/"EDIT-PATH/diagnostics.jsonl").read_text())
            self.assertEqual(line["api_key"], "<redacted>")
            out=create_bundle(root, root/"support.zip"); self.assertTrue(out.is_file())
