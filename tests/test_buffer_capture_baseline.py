import json
from pathlib import Path

from codetrace import buffer_capture, report
from codetrace.source import file_record
from codetrace.store import connect

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_dump_capture_changes_archive_for_same_window():
    db = connect(":memory:")
    try:
        items = json.loads((FIXTURES / "buffer_archive_baseline.json").read_text(encoding="utf-8"))["dumps"]
        for item, capture_state, archive_state in zip(
            items, ("full", "short_dump"), ("archivable", "needs_capture")
        ):
            payload = {"dumps": [item]}
            results = buffer_capture.import_data(db, payload)
            with db:
                file_record(db, "buffer-import", json.dumps(payload).encode("utf-8"))
            assert results == [{"id": item["id"], "capture": capture_state, "archive": archive_state}]
            assert buffer_capture.snapshot(db)[0]["state"] == archive_state
        assert report.audit(db)["ok"]
    finally:
        db.close()
