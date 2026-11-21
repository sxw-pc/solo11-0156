import json
from pathlib import Path

from codetrace import impact_capture, report
from codetrace.source import file_record
from codetrace.store import connect

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "impact_baseline.json"


def test_new_capture_replaces_transport_packet_classification():
    events = json.loads(FIXTURE.read_text(encoding="utf-8"))["captures"]
    db = connect(":memory:")
    try:
        for event, capture, status in zip(events, ("normal", "triggered"), ("ordinary_packet", "overload_packet")):
            payload = {"captures": [event]}
            assert impact_capture.import_data(db, payload) == [{"capture": capture, "case": status}]
            with db:
                file_record(db, "impact-import", json.dumps(payload).encode("utf-8"))
            assert impact_capture.snapshot(db) == [{"leg": "DEMO-LEG", "status": status, "evidence_id": event["id"]}]
        assert report.audit(db)["ok"]
    finally:
        db.close()
