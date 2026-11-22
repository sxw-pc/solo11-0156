import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from codetrace import buffer_reader, report
from codetrace.source import file_record
from codetrace.store import connect

ROOT = Path(__file__).resolve().parents[1]

SLOTS = 4
GENERATION = "writer-g1"


def request(req_id="ARCHIVE-X", start=4, end=8):
    return {"id": req_id, "slots": SLOTS, "start": start, "end": end, "generation": GENERATION}


def sweep(sweep_id, req_id="ARCHIVE-X", pointer=0, reads=None, generation=GENERATION):
    reads = reads or [
        {"slot": 0, "at": 10, "byte": "a"},
        {"slot": 1, "at": 11, "byte": "b"},
        {"slot": 2, "at": 12, "byte": "c"},
        {"slot": 3, "at": 13, "byte": "d"},
    ]
    return {
        "id": sweep_id,
        "request_id": req_id,
        "generation": generation,
        "pointer_start": pointer,
        "pointer_end": pointer,
        "reads": reads,
    }


def freeze(freeze_id, req_id="ARCHIVE-X", next_seq=8, reads=None, generation=GENERATION):
    reads = reads or [
        {"slot": 0, "at": 20, "byte": "a"},
        {"slot": 1, "at": 21, "byte": "b"},
        {"slot": 2, "at": 22, "byte": "c"},
        {"slot": 3, "at": 23, "byte": "d"},
    ]
    return {
        "id": freeze_id,
        "request_id": req_id,
        "generation": generation,
        "next_seq": next_seq,
        "stop_receipt": "STOP-" + freeze_id,
        "resume_receipt": "RESUME-" + freeze_id,
        "reads": reads,
    }


def mirror(mirror_id, seqs_bytes, req_id="ARCHIVE-X", generation=GENERATION):
    return {
        "id": mirror_id,
        "request_id": req_id,
        "generation": generation,
        "fragments": [
            {"seq": seq, "byte": byte, "receipt": "RCPT-%s-%d" % (mirror_id, seq), "at": 100 + seq}
            for seq, byte in seqs_bytes
        ],
    }


def import_payload(db, payload):
    results = buffer_reader.import_data(db, payload)
    file_record(db, "archive-import", json.dumps(payload).encode("utf-8"))
    return results


def verdict(db, request_id="ARCHIVE-X"):
    return next(v for v in buffer_reader.snapshot(db) if v["request_id"] == request_id)


@pytest.fixture()
def db():
    database = connect(":memory:")
    yield database
    database.close()


def test_live_sweep_alone_signs_nothing_ring_wrap_is_not_atomic(db):
    """ARCHIVE-X initial request: pointer 0->0 over a full lap is not atomic."""
    result = import_payload(
        db,
        {"archive_requests": [request()], "sweeps": [sweep("SWEEP-1")]},
    )
    v = result[0]
    assert v["state"] == "needs_capture"
    assert v["delivered"] == []
    assert [g["seq"] for g in v["gaps"]] == [4, 5, 6, 7]
    # The same cell bytes fit both laps; unattributed originals are retained.
    assert {o["seq"] for o in v["unattributed_originals"]} == {4, 5, 6, 7}
    assert {o["byte"] for o in v["unattributed_originals"]} == {"a", "b", "c", "d"}
    assert verdict(db)["state"] == "needs_capture"


def test_freeze_anchor_next_seq_8_proves_the_whole_interval_in_order(db):
    payload = {
        "archive_requests": [request()],
        "sweeps": [sweep("SWEEP-1")],
        "freezes": [freeze("FREEZE-1", next_seq=8)],
    }
    v = import_payload(db, payload)[0]
    assert v["state"] == "archived"
    assert [d["seq"] for d in v["delivered"]] == [4, 5, 6, 7]
    assert [d["byte"] for d in v["delivered"]] == ["a", "b", "c", "d"]
    assert all(d["generation"] == GENERATION for d in v["delivered"])
    assert all(d["routes"] == ["physical"] and d["sources"] == ["FREEZE-1"] for d in v["delivered"])
    assert v["gaps"] == []
    # Each delivered fragment carries its own recomputed digest.
    assert all(d["digest"] == buffer_reader.digest(d["byte"]) for d in v["delivered"])


def test_freeze_at_12_only_proves_8_to_11_and_leaves_4_to_8_a_gap(db):
    payload = {
        "archive_requests": [request()],
        "sweeps": [sweep("SWEEP-1")],
        "freezes": [freeze("FREEZE-12", next_seq=12)],
    }
    v = import_payload(db, payload)[0]
    assert v["state"] == "needs_capture"
    assert v["delivered"] == []
    assert [g["seq"] for g in v["gaps"]] == [4, 5, 6, 7]


def test_freeze_at_12_plus_historical_mirror_delivers_only_the_mirror_range(db):
    payload = {
        "archive_requests": [request()],
        "sweeps": [sweep("SWEEP-1")],
        "freezes": [freeze("FREEZE-12", next_seq=12)],
        "mirrors": [mirror("MIRROR-1", [(4, "a"), (5, "b"), (6, "c"), (7, "d")])],
    }
    v = import_payload(db, payload)[0]
    assert v["state"] == "archived"
    assert [d["seq"] for d in v["delivered"]] == [4, 5, 6, 7]
    assert [d["byte"] for d in v["delivered"]] == ["a", "b", "c", "d"]
    assert all(d["routes"] == ["mirror"] for d in v["delivered"])
    # The freeze cannot extend backward to fabricate the overwritten window.
    assert not any("FREEZE-12" in d["sources"] for d in v["delivered"])


def test_mirror_gap_is_not_filled_by_same_byte_re_read_with_matching_digest(db):
    # Mirror only witnessed 4, 5, 7; sequence 6 is the mirror's own lost
    # segment.  The physical window at sequences 8..11 happens to contain the
    # exact bytes of 6 in one slot; a digest match must not restore seq 6.
    physical_reads = [
        {"slot": 0, "at": 20, "byte": "a"},  # seq 8 == bytes of seq 4
        {"slot": 1, "at": 21, "byte": "b"},  # seq 9
        {"slot": 2, "at": 22, "byte": "c"},  # seq 10 == bytes of missing seq 6
        {"slot": 3, "at": 23, "byte": "x"},  # seq 11
    ]
    payload = {
        "archive_requests": [request()],
        "freezes": [freeze("FREEZE-12", next_seq=12, reads=physical_reads)],
        "mirrors": [mirror("MIRROR-1", [(4, "a"), (5, "b"), (7, "d")])],
    }
    v = import_payload(db, payload)[0]
    assert v["state"] == "partial"
    assert [d["seq"] for d in v["delivered"]] == [4, 5, 7]
    gap6 = next(g for g in v["gaps"] if g["seq"] == 6)
    assert gap6["reason"] == "no_proven_original"
    assert buffer_reader.digest("c") == buffer_reader.digest("c")  # equal digest is irrelevant
    assert all(d["seq"] != 6 for d in v["delivered"])


def test_late_restart_witness_links_evidence_from_the_next_generation(db):
    payload_before = {
        "archive_requests": [request()],
        "mirrors": [mirror("MIRROR-G2", [(4, "a"), (5, "b")], generation="writer-g2")],
    }
    before = import_payload(db, payload_before)[0]
    # Without the witness the later generation is not the same writer process.
    assert before["state"] == "needs_capture"
    assert before["invalid_evidence"] == [{"source": "MIRROR-G2", "reason": "restart_unlinked"}]
    assert [g["seq"] for g in before["gaps"]] == [4, 5, 6, 7]

    witness = {
        "id": "RESTART-1",
        "generation": GENERATION,
        "next_generation": "writer-g2",
        "next_seq": 4,
    }
    after = import_payload(db, {"restarts": [witness]})[0]
    assert after["state"] == "partial"
    assert [(d["seq"], d["byte"]) for d in after["delivered"]] == [(4, "a"), (5, "b")]
    assert [g["seq"] for g in after["gaps"]] == [6, 7]
    assert after["invalid_evidence"] == []


def test_common_order_conflict_between_sweeps_blocks_delivery(db):
    # A second sweep reads the same modulo pointer but different slot bytes in
    # a different order; no single candidate history explains both.
    second_reads = [
        {"slot": 0, "at": 30, "byte": "q"},
        {"slot": 1, "at": 31, "byte": "b"},
        {"slot": 2, "at": 32, "byte": "c"},
        {"slot": 3, "at": 33, "byte": "d"},
    ]
    payload = {
        "archive_requests": [request()],
        "sweeps": [sweep("SWEEP-1"), sweep("SWEEP-2", reads=second_reads)],
    }
    v = import_payload(db, payload)[0]
    assert v["delivered"] == []
    assert any(e["reason"] == "common_order_conflict" for e in v["invalid_evidence"])
    assert [g["seq"] for g in v["gaps"]] == [4, 5, 6, 7]


def test_pointer_movement_during_sweep_is_rejected(db):
    moving = sweep("SWEEP-MOVE")
    moving["pointer_end"] = 1
    v = import_payload(db, {"archive_requests": [request()], "sweeps": [moving]})[0]
    assert v["delivered"] == []
    assert v["invalid_evidence"] == [{"source": "SWEEP-MOVE", "reason": "pointer_moved"}]


def test_duplicate_real_commit_is_delivered_once_when_both_routes_agree(db):
    payload_a = {
        "archive_requests": [request()],
        "freezes": [freeze("FREEZE-1", next_seq=8)],
    }
    first = import_payload(db, payload_a)[0]
    payload_b = {"mirrors": [mirror("MIRROR-1", [(4, "a"), (5, "b"), (6, "c"), (7, "d")])]}
    second = import_payload(db, payload_b)[0]
    assert second["state"] == "archived"
    assert len(second["delivered"]) == 4
    delivered4 = next(d for d in second["delivered"] if d["seq"] == 4)
    assert delivered4["sources"] == ["FREEZE-1", "MIRROR-1"]
    assert delivered4["routes"] == ["mirror", "physical"]
    # Re-importing the same evidence is idempotent and still delivers once.
    again = import_payload(db, payload_b)[0]
    assert again == second
    assert first["state"] == "archived"


def test_route_attestation_conflict_keeps_both_originals_and_never_overwrites(db):
    payload_a = {
        "archive_requests": [request()],
        "freezes": [freeze("FREEZE-1", next_seq=8)],
    }
    import_payload(db, payload_a)
    payload_b = {"mirrors": [mirror("MIRROR-X", [(4, "z"), (5, "b"), (6, "c"), (7, "d")])]}
    v = import_payload(db, payload_b)[0]
    assert v["state"] == "conflict"
    conflict = v["conflicts"][0]
    assert conflict["seq"] == 4
    assert {o["byte"] for o in conflict["originals"]} == {"a", "z"}
    assert {o["source"] for o in conflict["originals"]} == {"FREEZE-1", "MIRROR-X"}
    assert all(d["seq"] != 4 for d in v["delivered"])
    # Import order cannot resolve the conflict: mirror first, then freeze.
    other = connect(":memory:")
    try:
        import_payload(
            other,
            {
                "archive_requests": [request()],
                "mirrors": [mirror("MIRROR-X", [(4, "z"), (5, "b"), (6, "c"), (7, "d")])],
                "freezes": [freeze("FREEZE-1", next_seq=8)],
            },
        )
        reversed_v = verdict(other)
        assert reversed_v["state"] == "conflict"
        assert {o["byte"] for o in reversed_v["conflicts"][0]["originals"]} == {"a", "z"}
    finally:
        other.close()


def test_replay_from_originals_reproduces_the_verdict(db):
    payloads = [
        {"archive_requests": [request()], "sweeps": [sweep("SWEEP-1")]},
        {"freezes": [freeze("FREEZE-12", next_seq=12)]},
        {"mirrors": [mirror("MIRROR-1", [(4, "a"), (5, "b"), (6, "c"), (7, "d")])]},
    ]
    for payload in payloads:
        import_payload(db, payload)
    assert report.audit(db)["ok"]
    replayed = verdict(db)
    assert replayed["state"] == "archived"
    assert [d["seq"] for d in replayed["delivered"]] == [4, 5, 6, 7]


def test_nonzero_pointer_first_lap_remains_ambiguous(db):
    req2 = {"id": "ARCHIVE-P2", "slots": 4, "start": 2, "end": 6, "generation": GENERATION}
    reads = [
        {"slot": 0, "at": 1, "byte": "a"},
        {"slot": 1, "at": 2, "byte": "b"},
        {"slot": 2, "at": 3, "byte": "c"},
        {"slot": 3, "at": 4, "byte": "d"},
    ]
    payload = {
        "archive_requests": [req2],
        "sweeps": [sweep("SWEEP-P2", "ARCHIVE-P2", pointer=2, reads=reads)],
    }
    v = import_payload(db, payload)[0]
    assert v["state"] == "needs_capture"
    assert v["delivered"] == []
    assert [g["seq"] for g in v["gaps"]] == [2, 3, 4, 5]


def test_append_only_mirror_rejects_two_binds_of_one_sequence(db):
    import_payload(db, {"archive_requests": [request()]})
    bad = {
        "id": "MIRROR-DUP",
        "request_id": "ARCHIVE-X",
        "generation": GENERATION,
        "fragments": [
            {"seq": 4, "byte": "a", "receipt": "r1", "at": 1},
            {"seq": 4, "byte": "b", "receipt": "r2", "at": 2},
        ],
    }
    with pytest.raises(ValueError, match="binds each sequence once"):
        buffer_reader.import_data(db, {"mirrors": [bad]})


def test_request_without_any_evidence_is_all_gaps(db):
    v = import_payload(db, {"archive_requests": [request()]})[0]
    assert v["state"] == "needs_capture"
    assert v["delivered"] == []
    assert [g["seq"] for g in v["gaps"]] == [4, 5, 6, 7]
    assert v["unattributed_originals"] == []


def test_late_witness_removes_stale_invalid_ledger_row(db):
    mirror_g2 = mirror("MIRROR-G2B", [(4, "a")], generation="writer-g2")
    import_payload(db, {"archive_requests": [request()], "mirrors": [mirror_g2]})
    rows = [dict(r) for r in db.execute("SELECT source_id, reason FROM archive_invalid")]
    assert rows == [{"source_id": "MIRROR-G2B", "reason": "restart_unlinked"}]
    witness = {
        "id": "RESTART-B",
        "generation": GENERATION,
        "next_generation": "writer-g2",
        "next_seq": 4,
    }
    import_payload(db, {"restarts": [witness]})
    assert [dict(r) for r in db.execute("SELECT source_id, reason FROM archive_invalid")] == []


def test_cli_archive_import_and_report_round_trip():
    with tempfile.TemporaryDirectory() as directory:
        database = str(Path(directory) / "state.sqlite")
        payload = {
            "archive_requests": [request()],
            "freezes": [freeze("FREEZE-1", next_seq=8)],
        }
        input_path = Path(directory) / "archive.json"
        input_path.write_text(json.dumps(payload), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, "-B", "-m", "codetrace", "archive-import", "--db", database, "--input", str(input_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr
        printed = json.loads(run.stdout)
        assert printed[0]["state"] == "archived"
        report_run = subprocess.run(
            [sys.executable, "-B", "-m", "codetrace", "archive-report", "--db", database],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert report_run.returncode == 0, report_run.stderr
        snap = json.loads(report_run.stdout)
        assert [d["seq"] for d in snap[0]["delivered"]] == [4, 5, 6, 7]
        audit_run = subprocess.run(
            [sys.executable, "-B", "-m", "codetrace", "audit", "--db", database],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        assert json.loads(audit_run.stdout)["ok"]
