import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from codetrace import impact_witness, report
from codetrace.source import file_record
from codetrace.store import connect

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "impact_witness.json"


def cases():
    return {case["id"]: case for case in
            json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]}


def evaluate(case_id):
    return impact_witness.evaluate_case(
        impact_witness.normalize_case(copy.deepcopy(cases()[case_id])))


def leg(result, leg_id):
    return next(x for x in result["legs"] if x["leg"] == leg_id)


def test_sync_anchor_recovers_real_concurrent_tick():
    result = evaluate("TRANSPORT-X-SYNC")
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] == "overload"
    assert transport["tick_known"] is True
    assert transport["overload_ticks"] == [1]
    assert transport["ticks"][0]["sum"] == 12
    assert transport["ticks"][0]["values"] == {"U": 6, "V": 6}
    assert transport["basis"] == "paired_runs"
    assert result["conflicts"] == []


def test_acceptance_transport_x_paired_route():
    """U is 6 at global tick 1; V is 6 at local tick 0.

    V may be aligned at local+0 (no crossing) or local+1 (tick 1 crosses).
    The sync anchor's offset 1 and trusted handover tick must pick one whole
    history and output 12 at tick 1, not simply archive the two 6s.
    """
    case = {
        "id": "TRANSPORT-X-ACCEPT", "limit": 10,
        "legs": [{"id": "TRANSPORT-X", "start_tick": 0, "end_tick": 2}],
        "runs": [
            {"id": "U", "device": "U", "round": "R1", "offset": 0,
             "samples": [{"tick": 0, "value": 0}, {"tick": 1, "value": 6},
                         {"tick": 2, "value": 0}]},
            {"id": "V", "device": "V", "round": "R1",
             "allowed_offsets": [0, 1],
             "samples": [{"tick": 0, "value": 6}, {"tick": 1, "value": 0},
                         {"tick": 2, "value": 0}]},
        ],
        "syncs": [{"id": "SYNC", "a_run": "U", "b_run": "V",
                   "a_tick": 1, "b_tick": 0, "offset": 1, "tick": 1}],
    }
    result = impact_witness.evaluate_case(impact_witness.normalize_case(case))
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] == "overload"
    assert transport["tick_known"] is True
    assert transport["overload_ticks"] == [1]
    assert transport["ticks"] == [
        {"tick": 1, "sum": 12, "values": {"U": 6, "V": 6}}]
    # The alternative offset 0 history must not leak into the signed packet.
    assert all(h["offsets"] == {"U": 0, "V": 1}
               for h in transport["hypotheses"])

    without_sync = copy.deepcopy(case)
    without_sync.pop("syncs")
    result = impact_witness.evaluate_case(
        impact_witness.normalize_case(without_sync))
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] != "overload"
    assert transport["overload_ticks"] == []


def test_two_local_peaks_alone_are_not_an_overload():
    case = cases()["TRANSPORT-X-SYNC"]
    case.pop("syncs")
    result = impact_witness.evaluate_case(impact_witness.normalize_case(case))
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] != "overload"
    assert transport["tick_known"] is False
    assert transport["overload_ticks"] == []
    assert any("sync" in item for item in transport["missing_observations"])


def test_missing_sample_is_not_zero_filled_and_is_reported():
    case = copy.deepcopy(cases()["TRANSPORT-X-SYNC"])
    # Drop V's local tick -1, which maps to global tick 0 under the pinned
    # offset.  Global tick 1 (the crossing) must remain observed.
    case["runs"][1]["samples"] = [
        {"tick": 0, "value": 6}, {"tick": 1, "value": 0}]
    result = impact_witness.evaluate_case(impact_witness.normalize_case(case))
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] == "overload"
    assert transport["overload_ticks"] == [1]
    assert transport["missing_ticks"] == [0]
    assert {"leg": "TRANSPORT-X", "tick": 0, "devices": ["V"]} in \
        transport["missing_samples"]
    assert any("unobserved rather than zero" in item
               for item in transport["missing_observations"])


def test_one_offset_serves_the_whole_run_not_each_peak():
    case = {
        "id": "ONE-OFFSET", "limit": 10,
        "legs": [{"id": "TRANSPORT-X", "start_tick": 0, "end_tick": 2}],
        "runs": [
            {"id": "U", "device": "U", "round": "R1", "offset": 0,
             "samples": [{"tick": 0, "value": 6}, {"tick": 1, "value": 6},
                         {"tick": 2, "value": 0}]},
            {"id": "V", "device": "V", "round": "R1", "allowed_offsets": [0, 1],
             "samples": [{"tick": 0, "value": 6}, {"tick": 1, "value": 0}]},
        ],
        "syncs": [{"id": "S", "a_run": "U", "b_run": "V",
                   "a_tick": 0, "b_tick": 0, "tick": 0}],
    }
    result = impact_witness.evaluate_case(impact_witness.normalize_case(case))
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] == "overload"
    assert transport["overload_ticks"] == [0]
    assert [h["offsets"] for h in transport["hypotheses"]] == \
        [{"U": 0, "V": 0}]


def test_witness_strip_signs_phase_only():
    result = evaluate("TRANSPORT-X-STRIP")
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] == "overload_phase"
    assert transport["tick_known"] is False
    assert transport["overload_ticks"] == []
    assert transport["basis"] == "witness_strip"
    assert any("flip tick" in item for item in transport["missing_observations"])


def test_strip_flipped_at_placement_or_incomplete_swap_is_unusable():
    pre = leg(evaluate("TRANSPORT-X-STRIP-PREFLIPPED"), "TRANSPORT-X")
    assert pre["status"] == "unverified"
    assert pre["strip"]["usable"] == []
    assert any("already changed at placement" in item["reason"]
               for item in pre["strip"]["unusable"])

    swapped = leg(evaluate("TRANSPORT-X-STRIP-SWAPPED"), "TRANSPORT-X")
    assert swapped["status"] == "unverified"
    assert swapped["strip"]["usable"] == []
    assert any("replacement record is incomplete" in item["reason"]
               for item in swapped["strip"]["unusable"])


def test_strip_contradicting_paired_series_keeps_both():
    case = copy.deepcopy(cases()["TRANSPORT-X-SYNC"])
    case["strips"] = [{
        "id": "STRIP-OPPOSITE", "leg": "TRANSPORT-X", "placement_tick": 0,
        "recovery_tick": 2, "placed_state": "intact",
        "recovered_state": "intact", "placement_complete": True,
        "recovery_complete": True, "leg_start_ok": True, "leg_end_ok": True,
    }]
    result = impact_witness.evaluate_case(impact_witness.normalize_case(case))
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] == "conflict"
    assert transport["tick_known"] is False
    assert any(c["kind"] == "paired_strip_conflict" for c in result["conflicts"])
    assert transport["strip"]["usable"] == ["STRIP-OPPOSITE"]


def test_two_legs_without_a_shared_history_are_not_cross_cited():
    """Each peak fits one leg under a different whole-run offset.

    Neither leg may be signed: no single history makes both legs cross.
    """
    result = evaluate("TRANSPORT-X-REBOOT-CONFLICT")
    assert all(x["status"] == "conflict" for x in result["legs"])
    assert all(x["tick_known"] is False for x in result["legs"])
    assert all(x["overload_ticks"] == [] for x in result["legs"])
    # Each peak is explainable under a different per-run offset; the
    # candidate ticks are kept but never signed.
    assert {x["possible_ticks"][0] for x in result["legs"]} == {1, 3}
    assert [c["kind"] for c in result["conflicts"]].count("no_common_history") == 2


def test_receipt_tick_phase_and_sampling_are_separated():
    result = evaluate("TRANSPORT-X-RECEIPT")
    transport = leg(result, "TRANSPORT-Y")
    assert transport["status"] == "overload"
    assert transport["overload_ticks"] == [2]
    entry = transport["receipts"][0]
    assert entry["id"] == "RECEIPT-Y"
    assert entry["leg_trusted"] is True
    assert entry["tick_trusted"] is True
    assert entry["sampling_preserves_concurrency"] is True

    lossy = copy.deepcopy(cases()["TRANSPORT-X-RECEIPT"])
    lossy["receipts"][0]["sampling"] = "peak_summary"
    result = impact_witness.evaluate_case(impact_witness.normalize_case(lossy))
    transport = leg(result, "TRANSPORT-Y")
    assert any("concurrent" in item or "preserves concurrent" in item
               for item in transport["missing_observations"])


def test_same_strip_cannot_sign_two_legs():
    db = connect(":memory:")
    try:
        first = copy.deepcopy(cases()["TRANSPORT-X-STRIP"])
        impact_witness.import_data(db, first)
        second = copy.deepcopy(cases()["TRANSPORT-X-STRIP"])
        second["id"] = "TRANSPORT-X-STRIP-MOVED"
        second["legs"] = [{"id": "TRANSPORT-Y", "start_tick": 0, "end_tick": 2}]
        second["strips"][0]["leg"] = "TRANSPORT-Y"
        result = impact_witness.import_data(db, second)
        moved = leg(result, "TRANSPORT-Y")
        assert moved["status"] == "unverified"
        assert moved["strip"]["usable"] == []
        assert any("cannot sign two legs" in item
                   for item in moved["missing_observations"])
        assert impact_witness.snapshot(db)["strips"][0]["leg"] == "TRANSPORT-X"
    finally:
        db.close()


def test_already_produced_packet_and_late_conflict_are_both_kept():
    db = connect(":memory:")
    try:
        overload = {
            "id": "DOC-LATE-A", "limit": 10,
            "legs": [{"id": "TRANSPORT-X", "start_tick": 0, "end_tick": 2}],
            "runs": [
                {"id": "U", "device": "U", "round": "R1", "offset": 0,
                 "samples": [{"tick": 0, "value": 0}, {"tick": 1, "value": 6},
                             {"tick": 2, "value": 0}]},
                {"id": "V", "device": "V", "round": "R1", "offset": 0,
                 "samples": [{"tick": 0, "value": 0}, {"tick": 1, "value": 6},
                             {"tick": 2, "value": 0}]},
            ],
            "syncs": [{"id": "S-A", "a_run": "U", "b_run": "V",
                       "a_tick": 1, "b_tick": 1, "tick": 1}],
        }
        normal = copy.deepcopy(overload)
        normal["id"] = "DOC-LATE-B"
        normal["runs"][0]["samples"][1]["value"] = 0
        normal["runs"][1]["samples"][1]["value"] = 0
        normal["syncs"][0]["id"] = "S-B"

        first = impact_witness.import_data(db, overload)
        assert leg(first, "TRANSPORT-X")["status"] == "overload"
        second = impact_witness.import_data(db, normal)
        assert leg(second, "TRANSPORT-X")["status"] == "conflict"
        assert second["preserved"] == ["DOC-LATE-A:TRANSPORT-X"]

        packets = {p["id"]: p for p in impact_witness.snapshot(db)["packets"]}
        assert packets["DOC-LATE-A:TRANSPORT-X"]["status"] == "overload"
        assert packets["DOC-LATE-A:TRANSPORT-X"]["tick_known"] == 1
        assert packets["DOC-LATE-B:TRANSPORT-X"]["status"] == "conflict"
        conflicts = impact_witness.snapshot(db)["conflicts"]
        assert any(c["kind"] == "late_evidence_conflict" for c in conflicts)
    finally:
        db.close()


def test_audit_replays_witness_originals_from_files():
    db = connect(":memory:")
    try:
        for name, payload in (("t1", cases()["TRANSPORT-X-SYNC"]),
                              ("t2", cases()["TRANSPORT-X-STRIP"])):
            doc = copy.deepcopy(payload)
            impact_witness.import_data(db, doc)
            with db:
                file_record(db, "witness-import",
                            json.dumps(doc, ensure_ascii=False).encode("utf-8"))
        assert report.audit(db)["ok"]
    finally:
        db.close()


def test_cli_imports_reports_and_audits_witness_document():
    with tempfile.TemporaryDirectory() as directory:
        db = str(Path(directory) / "witness.sqlite")
        source = Path(directory) / "case.json"
        source.write_text(json.dumps(cases()["TRANSPORT-X-SYNC"],
                                     ensure_ascii=False), encoding="utf-8")
        runs = [
            ("witness-import", "--input", str(source)),
            ("witness-report",),
            ("audit",),
        ]
        for args in runs:
            result = subprocess.run(
                [sys.executable, "-B", "-m", "codetrace", args[0],
                 "--db", db, *args[1:]],
                cwd=ROOT, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            output = json.loads(result.stdout)
            if args[0] == "witness-report":
                assert output["packets"][0]["status"] == "overload"
                assert json.loads(output["packets"][0]["ticks"])[0]["tick"] == 1
            if args[0] == "audit":
                assert output["ok"] is True


def test_later_round_calibration_never_backfills_the_old_run():
    """A reboot splits the recording into rounds R1 and R2.

    The R2 offset is known; R1 is not.  The old peak must not be interpreted
    with the later calibration.
    """
    case = {
        "id": "REBOOT-BACKFILL", "limit": 10,
        "legs": [{"id": "TRANSPORT-X", "start_tick": 0, "end_tick": 2}],
        "runs": [
            {"id": "U-R1", "device": "U", "round": "R1", "offset": 0,
             "samples": [{"tick": 0, "value": 0}, {"tick": 1, "value": 6},
                         {"tick": 2, "value": 0}]},
            {"id": "V-R1", "device": "V", "round": "R1",
             "samples": [{"tick": 0, "value": 6}, {"tick": 1, "value": 0},
                         {"tick": 2, "value": 0}]},
            {"id": "V-R2", "device": "V", "round": "R2", "offset": 1,
             "samples": [{"tick": 0, "value": 6}, {"tick": 1, "value": 0},
                         {"tick": 2, "value": 0}]},
        ],
        "syncs": [],
    }
    result = impact_witness.evaluate_case(impact_witness.normalize_case(case))
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] != "overload"
    assert transport["tick_known"] is False
    assert any("back-fill" in item or "cannot" in item
               for item in transport["missing_observations"])


def test_strip_signs_phase_when_the_common_clock_was_lost():
    case = copy.deepcopy(cases()["TRANSPORT-X-SYNC"])
    case.pop("syncs")
    case["runs"][1].pop("allowed_offsets")
    case["runs"][0].pop("offset")
    case["strips"] = [{
        "id": "STRIP-PHASE", "leg": "TRANSPORT-X", "placement_tick": 0,
        "recovery_tick": 2, "placed_state": "intact",
        "recovered_state": "flipped", "placement_complete": True,
        "recovery_complete": True, "leg_start_ok": True, "leg_end_ok": True,
    }]
    result = impact_witness.evaluate_case(impact_witness.normalize_case(case))
    transport = leg(result, "TRANSPORT-X")
    assert transport["status"] == "overload_phase"
    assert transport["tick_known"] is False
    assert transport["basis"] == "witness_strip"
    assert transport["overload_ticks"] == []


def test_receipt_without_tick_or_leg_is_not_guessed():
    case = copy.deepcopy(cases()["TRANSPORT-X-RECEIPT"])
    case["receipts"] = [{"id": "RECEIPT-LOST", "sampling": "continuous",
                         "claims_overload": "overload"}]
    result = impact_witness.evaluate_case(impact_witness.normalize_case(case))
    both = result["legs"]
    assert all(not x["receipts"] for x in both)
    assert any("phase and sampling round must be solved together" in item
               for x in both for item in x["missing_observations"])


def test_bundle_import_is_idempotent_and_replayable():
    db = connect(":memory:")
    try:
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        first = impact_witness.import_data(db, copy.deepcopy(bundle))
        assert len(first) == len(bundle["cases"])
        before = impact_witness.snapshot(db)
        impact_witness.import_data(db, copy.deepcopy(bundle))
        assert impact_witness.snapshot(db) == before
        with db:
            file_record(db, "witness-import",
                        json.dumps(bundle, ensure_ascii=False).encode("utf-8"))
        assert report.audit(db)["ok"]
    finally:
        db.close()
