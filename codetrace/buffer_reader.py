"""Extract deliverable original fragments from evidence about a ring buffer.

The device exposes a bounded write ring: every received fragment is written
into the next slot and a full lap overwrites the oldest fragment.  The device
write pointer is the next-write slot *modulo* the ring length, so it is not an
absolute sequence number.  A reader cannot stop the writer while it scans,
therefore one scan may observe fragments belonging to different laps.

Two fully authorized acquisition routes may attest originals for an archive
request (an absolute, half-open sequence interval of one writer process):

* a frozen sweep: an allowed acquisition stop seals an absolute anchor (the next
  absolute sequence), with consistent stop/resume receipts, and the physical
  window is scanned while the writer is stopped;
* an append-only mirror: the software observation platform witnesses each
  committed write at commit time and binds a per-process commit receipt to the
  fragment bytes; it may itself lose segments.

The tool never promotes a fragment on bytes or digest alone.  The same bytes
may be collected twice in two separate real commits, and a digest match between
a mirror gap and a physical re-read does not restore the missing occurrence.
A real (sequence, fragment) pair is only delivered when every candidate write
history compatible with the pointers and the read timing proves the same pair.

Scope: rings of four slots, at most one writer restart, and candidate histories
of at most two laps.
"""

import hashlib
import json

from .source import record
from .store import history


def digest(payload):
    """Per-fragment recomputed digest; a manifest-level digest is never enough."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _require(obj, keys, kind, item_id):
    if not isinstance(obj, dict):
        raise ValueError("invalid %s record: %s" % (kind, item_id))
    for key in keys:
        if key not in obj:
            raise ValueError("invalid %s record %s: missing %s" % (kind, item_id, key))


def _validate_request(item):
    _require(item, ("id", "slots", "start", "end", "generation"), "archive-request", item.get("id"))
    if not isinstance(item["slots"], int) or item["slots"] <= 0:
        raise ValueError("invalid ring length: %r" % (item["slots"],))
    if (
        not isinstance(item["start"], int)
        or not isinstance(item["end"], int)
        or item["start"] < 0
        or item["end"] < item["start"]
    ):
        raise ValueError("invalid absolute interval: %r" % (item["id"],))
    if not isinstance(item["generation"], str) or not item["generation"]:
        raise ValueError("invalid writer generation: %r" % (item["id"],))


def _validate_restart(item):
    _require(item, ("id", "generation", "next_generation", "next_seq"), "restart-witness", item.get("id"))
    if not isinstance(item["next_seq"], int) or item["next_seq"] < 0:
        raise ValueError("invalid restart next_seq: %r" % (item["id"],))
    for key in ("generation", "next_generation"):
        if not isinstance(item[key], str) or not item[key]:
            raise ValueError("invalid restart generation: %r" % (item["id"],))
    if item["generation"] == item["next_generation"]:
        raise ValueError("restart witness must name distinct generations: %r" % (item["id"],))


def _validate_reads(item, kind, count):
    if not isinstance(item["reads"], list) or not item["reads"]:
        raise ValueError("invalid %s reads: %r" % (kind, item["id"],))
    if len(item["reads"]) != count:
        raise ValueError("%s must read every ring slot exactly once: %r" % (kind, item["id"],))
    last = None
    slots = []
    for read in item["reads"]:
        _require(read, ("slot", "at", "byte"), kind + "-read", item["id"])
        if not isinstance(read["slot"], int) or not 0 <= read["slot"] < count:
            raise ValueError("invalid %s slot: %r" % (kind, item["id"],))
        if last is not None and read["at"] <= last:
            raise ValueError("%s reads must be strictly ordered by time: %r" % (kind, item["id"],))
        last = read["at"]
        slots.append(read["slot"])
    if sorted(slots) != list(range(count)):
        raise ValueError("%s must read every ring slot exactly once: %r" % (kind, item["id"],))


def _validate_sweep(item):
    _require(item, ("id", "request_id", "pointer_start", "pointer_end", "reads"), "sweep", item.get("id"))
    n = len(item["reads"])
    for key in ("pointer_start", "pointer_end"):
        if not isinstance(item[key], int) or not 0 <= item[key] < n:
            raise ValueError("invalid sweep pointer: %r" % (item["id"],))
    _validate_reads(item, "sweep", n)


def _validate_freeze(item):
    _require(item, ("id", "request_id", "next_seq", "stop_receipt", "resume_receipt", "reads"), "freeze", item.get("id"))
    n = len(item["reads"])
    if not isinstance(item["next_seq"], int) or item["next_seq"] < 0:
        raise ValueError("invalid freeze next_seq: %r" % (item["id"],))
    if not isinstance(item["stop_receipt"], str) or not item["stop_receipt"]:
        raise ValueError("freeze lacks a stop receipt: %r" % (item["id"],))
    if not isinstance(item["resume_receipt"], str) or not item["resume_receipt"]:
        raise ValueError("freeze lacks a resume receipt: %r" % (item["id"],))
    _validate_reads(item, "freeze", n)


def _validate_mirror(item):
    _require(item, ("id", "request_id", "fragments"), "mirror", item.get("id"))
    if not isinstance(item["fragments"], list) or not item["fragments"]:
        raise ValueError("invalid mirror fragments: %r" % (item["id"],))
    seen = set()
    last = None
    for fragment in item["fragments"]:
        _require(fragment, ("seq", "byte", "receipt", "at"), "mirror-fragment", item["id"])
        if not isinstance(fragment["seq"], int) or fragment["seq"] < 0:
            raise ValueError("invalid mirror sequence: %r" % (item["id"],))
        if fragment["seq"] in seen:
            raise ValueError("append-only mirror binds each sequence once: %r" % (item["id"],))
        seen.add(fragment["seq"])
        if not isinstance(fragment["receipt"], str) or not fragment["receipt"]:
            raise ValueError("mirror fragment lacks a commit receipt: %r" % (item["id"],))
        if last is not None and fragment["at"] <= last:
            raise ValueError("mirror fragments must be append-only ordered by time: %r" % (item["id"],))
        last = fragment["at"]


# ---------------------------------------------------------------------------
# Candidate histories (at most two laps around the ring)
# ---------------------------------------------------------------------------

def _candidate_maps(n, pointer):
    """Lap-uniform candidates compatible with one modulo pointer.

    A history of K commits has next-write slot ``K mod n``.  Within the two-lap
    scope the compatible histories are the first lap (the ring may never have
    wrapped yet), one full lap and two full laps; each candidate maps every read
    slot to one absolute sequence.  Slots from different candidates are never
    stitched into one package.
    """
    candidates = []
    for commits in (pointer, pointer + n, pointer + 2 * n):
        mapping = {}
        for slot in range(n):
            if commits < n:
                # First lap: slots below the pointer hold seq 0..K-1, the rest
                # have never been written.
                mapping[slot] = slot if slot < commits else None
            else:
                # A lap finished at seq K-1; the window holds K-n..K-1.
                mapping[slot] = commits - n + slot
        candidates.append({"commits": commits, "mapping": mapping, "source": None})
    return candidates


def _sweep_candidates(item, n):
    """Return surviving candidate mappings for one live (unfrozen) sweep."""
    if item["pointer_start"] != item["pointer_end"]:
        return None, "pointer_moved"
    by_slot = {read["slot"]: read["byte"] for read in item["reads"]}
    candidates = []
    for candidate in _candidate_maps(n, item["pointer_start"]):
        mapping = {}
        for slot, seq in candidate["mapping"].items():
            if seq is None:
                mapping[slot] = (None, by_slot[slot])  # never written in this history
            else:
                mapping[slot] = (seq, by_slot[slot])
        candidates.append(
            {
                "commits": candidate["commits"],
                "mapping": mapping,
                "generation": item.get("generation"),
                "source": None,
            }
        )
    return candidates, None


def _intersect(acc, incoming):
    """Keep candidates compatible on every slot both of them write.

    Unwritten cells (``None`` sequence in a first-lap history) carry no claim
    and never veto another candidate's claim on that slot; two candidates match
    when, for every slot where both make a claim, the (sequence, byte) pairs
    are identical.
    """
    def claims(mapping):
        return {slot: pair for slot, pair in mapping.items() if pair[0] is not None}

    result = []
    for candidate in incoming:
        wanted = claims(candidate["mapping"])
        for other in acc:
            known = claims(other["mapping"])
            shared = wanted.keys() & known.keys()
            if shared and all(known[slot] == wanted[slot] for slot in shared):
                result.append(candidate)
                break
    return result


def _freeze_claims(item, n):
    """Absolute claims of a frozen sweep; the anchor rules out every other lap."""
    generation = item.get("generation")
    if item["next_seq"] % n != 0:
        return None, "anchor_unaligned"
    by_slot = {read["slot"]: read["byte"] for read in item["reads"]}
    mapping = {slot: (item["next_seq"] - n + slot, by_slot[slot]) for slot in range(n)}
    return [{"mapping": mapping, "generation": generation, "source": item["id"]}], None


# ---------------------------------------------------------------------------
# Verdict construction
# ---------------------------------------------------------------------------

def _evaluate(request, sweeps, freezes, mirrors, restarts):
    n = request["slots"]
    start, end = request["start"], request["end"]
    invalids = []

    # Restart linkage: evidence of a later generation only counts once an
    # explicit witness chains it to the request's writer generation.
    linked = {request["generation"]}
    for witness in restarts:
        if witness["generation"] in linked:
            linked.add(witness["next_generation"])
        else:
            invalids.append({"source": witness["id"], "reason": "restart_unlinked"})
    for item in sweeps + freezes + mirrors:
        generation = item.get("generation")
        if generation is not None and generation not in linked:
            invalids.append({"source": item["id"], "reason": "restart_unlinked"})

    # Physical route.  Live sweeps retain every lap-compatible candidate; a
    # freeze seals an absolute anchor.  The two kinds only combine when they
    # agree on every slot's (sequence, byte); otherwise the conflict stands.
    sweep_candidates = []
    for item in sweeps:
        generation = item.get("generation")
        if generation is not None and generation not in linked:
            continue  # linkage failure is already listed in invalids
        candidates, reason = _sweep_candidates(item, n)
        if reason:
            invalids.append({"source": item["id"], "reason": reason})
            continue
        if not sweep_candidates:
            sweep_candidates = candidates
            continue
        surviving = []
        for history in sweep_candidates:
            if _intersect([history], candidates):
                surviving.append(history)
        if not surviving:
            invalids.append({"source": item["id"], "reason": "common_order_conflict"})
        else:
            sweep_candidates = surviving
    frozen = []
    for item in freezes:
        generation = item.get("generation")
        if generation is not None and generation not in linked:
            continue  # linkage failure is already listed in invalids
        claims, reason = _freeze_claims(item, n)
        if reason:
            invalids.append({"source": item["id"], "reason": reason})
            continue
        if not frozen:
            frozen = claims
            continue
        surviving = [history for history in frozen if _intersect([history], claims)]
        if not surviving:
            invalids.append({"source": item["id"], "reason": "common_order_conflict"})
        else:
            frozen = surviving
    if sweep_candidates and frozen:
        # An absolute anchor rules out first-lap histories with the same modulo
        # pointer; only the wrapped-lap sweep candidates can meet the anchor.
        wrapped = [c for c in sweep_candidates if c["commits"] >= n]
        physical = _intersect(wrapped, frozen)
        if not physical:
            invalids.append({"source": frozen[0]["source"], "reason": "common_order_conflict"})
    else:
        physical = sweep_candidates or frozen

    claims_by_seq = {}
    ambiguous = {}  # slot -> (byte, target seqs the observed byte may be)

    def add_claim(claim):
        bucket = claims_by_seq.setdefault(claim["seq"], [])
        if not any(c["source"] == claim["source"] and c["route"] == claim["route"] for c in bucket):
            bucket.append(claim)

    if physical:
        for slot in sorted(physical[0]["mapping"]):
            pairs = [candidate["mapping"][slot] for candidate in physical]
            proven = set(pairs)
            if len(proven) == 1 and next(iter(proven))[0] is not None:
                # Every surviving candidate proves the same real occurrence.
                seq, byte = next(iter(proven))
                if start <= seq < end:
                    sources = sorted({c["source"] for c in physical if c["source"]}) or ["physical-sweep"]
                    add_claim(
                        {
                            "seq": seq,
                            "byte": byte,
                            "source": sources[0],
                            "route": "physical",
                            "generation": physical[0]["generation"],
                        }
                    )
            else:
                # Ambiguous cell: the histories name different occurrence
                # counts (or an unwritten first-lap history).  The same slot in
                # the next lap is n commits later, so gather every target-range
                # sequence at which this observed byte could have occurred; the
                # assembly retains the original without signing an occurrence.
                byte = next(iter({value for _, value in proven}))
                seqs = {seq for seq, _ in proven if seq is not None}
                seqs |= {seq + n for seq in seqs}
                seqs |= {seq - n for seq in seqs}
                ambiguous[slot] = (byte, {seq for seq in seqs if start <= seq < end})

    # Mirror route: commit-time receipts bind absolute sequence to bytes.  A
    # lost mirror segment stays a gap; a physical re-read with the same digest
    # is not allowed to fill it.
    for item in mirrors:
        generation = item.get("generation")
        if generation is not None and generation not in linked:
            continue  # linkage failure is already listed in invalids
        for fragment in item["fragments"]:
            seq = fragment["seq"]
            if start <= seq < end:
                add_claim(
                    {
                        "seq": seq,
                        "byte": fragment["byte"],
                        "source": item["id"],
                        "route": "mirror",
                        "generation": generation,
                    }
                )

    delivered, gaps, conflicts, originals = [], [], [], []
    for seq in range(start, end):
        bucket = claims_by_seq.get(seq, [])
        if not bucket:
            # An ambiguous physical cell may hold this sequence's original, but
            # no candidate history proves the occurrence; retain the observed
            # byte next to the gap without signing it.
            for slot in sorted(ambiguous):
                byte, seqs = ambiguous[slot]
                if seq in seqs:
                    originals.append(
                        {
                            "seq": seq,
                            "byte": byte,
                            "digest": digest(byte),
                            "sources": ["physical-sweep"],
                            "status": "unattributed_original",
                        }
                    )
                    break
            gaps.append({"seq": seq, "reason": "no_proven_original"})
            continue
        bytes_seen = {c["byte"] for c in bucket}
        if len(bytes_seen) > 1:
            # Contradictory attested originals: retain both; local import order
            # must never overwrite one with the other.
            conflicts.append(
                {
                    "seq": seq,
                    "reason": "attestation_conflict",
                    "originals": [
                        {
                            "byte": c["byte"],
                            "digest": digest(c["byte"]),
                            "source": c["source"],
                            "route": c["route"],
                        }
                        for c in sorted(bucket, key=lambda c: (c["source"], c["byte"]))
                    ],
                }
            )
            continue
        winner = bucket[0]
        delivered.append(
            {
                "seq": seq,
                "byte": winner["byte"],
                "digest": digest(winner["byte"]),
                "generation": winner["generation"],
                "sources": sorted({c["source"] for c in bucket}),
                "routes": sorted({c["route"] for c in bucket}),
            }
        )

    if conflicts:
        state = "conflict"
    elif not delivered:
        state = "needs_capture"
    elif len(delivered) == end - start:
        state = "archived"
    else:
        state = "partial"
    return {
        "request_id": request["id"],
        "state": state,
        "delivered": delivered,
        "gaps": gaps,
        "unattributed_originals": originals,
        "conflicts": conflicts,
        "invalid_evidence": sorted(invalids, key=lambda x: (x["source"], x["reason"])),
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_request(
    id TEXT PRIMARY KEY, slots INTEGER NOT NULL,
    start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL,
    generation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_sweep(
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
    generation TEXT, pointer_start INTEGER NOT NULL,
    pointer_end INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS archive_sweep_read(
    sweep_id TEXT NOT NULL, slot INTEGER NOT NULL,
    at_order INTEGER NOT NULL, byte TEXT NOT NULL,
    PRIMARY KEY(sweep_id, slot));
CREATE TABLE IF NOT EXISTS archive_freeze(
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
    generation TEXT, next_seq INTEGER NOT NULL,
    stop_receipt TEXT NOT NULL, resume_receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_freeze_read(
    freeze_id TEXT NOT NULL, slot INTEGER NOT NULL,
    at_order INTEGER NOT NULL, byte TEXT NOT NULL,
    PRIMARY KEY(freeze_id, slot));
CREATE TABLE IF NOT EXISTS archive_mirror(
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL, generation TEXT);
CREATE TABLE IF NOT EXISTS archive_mirror_fragment(
    mirror_id TEXT NOT NULL, seq INTEGER NOT NULL,
    byte TEXT NOT NULL, receipt TEXT NOT NULL, at_order INTEGER NOT NULL,
    PRIMARY KEY(mirror_id, seq));
CREATE TABLE IF NOT EXISTS archive_restart(
    id TEXT PRIMARY KEY, generation TEXT NOT NULL,
    next_generation TEXT NOT NULL, next_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS archive_invalid(
    source_id TEXT NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY(source_id, reason));
CREATE TABLE IF NOT EXISTS archive_verdict(
    request_id TEXT PRIMARY KEY, state TEXT NOT NULL, verdict TEXT NOT NULL);
"""


def _load(db, request_id):
    request = db.execute("SELECT * FROM archive_request WHERE id=?", (request_id,)).fetchone()
    if not request:
        return None
    sweeps = [dict(r) for r in db.execute("SELECT * FROM archive_sweep WHERE request_id=?", (request_id,))]
    for sweep in sweeps:
        sweep["reads"] = [
            {"slot": r["slot"], "at": r["at_order"], "byte": r["byte"]}
            for r in db.execute(
                "SELECT slot,at_order,byte FROM archive_sweep_read WHERE sweep_id=? ORDER BY at_order",
                (sweep["id"],),
            )
        ]
    freezes = [dict(r) for r in db.execute("SELECT * FROM archive_freeze WHERE request_id=?", (request_id,))]
    for freeze in freezes:
        freeze["reads"] = [
            {"slot": r["slot"], "at": r["at_order"], "byte": r["byte"]}
            for r in db.execute(
                "SELECT slot,at_order,byte FROM archive_freeze_read WHERE freeze_id=? ORDER BY at_order",
                (freeze["id"],),
            )
        ]
    mirrors = [dict(r) for r in db.execute("SELECT * FROM archive_mirror WHERE request_id=?", (request_id,))]
    for mirror in mirrors:
        mirror["fragments"] = [
            {"seq": r["seq"], "byte": r["byte"], "receipt": r["receipt"], "at": r["at_order"]}
            for r in db.execute(
                "SELECT seq,byte,receipt,at_order FROM archive_mirror_fragment WHERE mirror_id=? ORDER BY at_order",
                (mirror["id"],),
            )
        ]
    restarts = [dict(r) for r in db.execute("SELECT * FROM archive_restart ORDER BY next_seq,id")]
    return {
        "request": {
            "id": request["id"],
            "slots": request["slots"],
            "start": request["start_seq"],
            "end": request["end_seq"],
            "generation": request["generation"],
        },
        "sweeps": sweeps,
        "freezes": freezes,
        "mirrors": mirrors,
        "restarts": restarts,
    }


def import_data(db, data):
    """Import archive requests and their evidence, then recompute verdicts."""
    db.executescript(_SCHEMA)
    touched = []
    with db:
        for item in data.get("archive_requests", []):
            _validate_request(item)
            if record(db, "archive-request", item):
                db.execute(
                    "INSERT INTO archive_request VALUES(?,?,?,?,?)",
                    (item["id"], item["slots"], item["start"], item["end"], item["generation"]),
                )
            touched.append(item["id"])
        for item in data.get("restarts", []):
            _validate_restart(item)
            if record(db, "restart-witness", item):
                db.execute(
                    "INSERT INTO archive_restart VALUES(?,?,?,?)",
                    (item["id"], item["generation"], item["next_generation"], item["next_seq"]),
                )
            touched.extend(
                r[0]
                for r in db.execute(
                    "SELECT id FROM archive_request WHERE generation=? OR generation=?",
                    (item["generation"], item["next_generation"]),
                )
            )
        for item in data.get("sweeps", []):
            _validate_sweep(item)
            if record(db, "sweep", item):
                db.execute(
                    "INSERT INTO archive_sweep VALUES(?,?,?,?,?)",
                    (item["id"], item["request_id"], item.get("generation"), item["pointer_start"], item["pointer_end"]),
                )
                for read in item["reads"]:
                    db.execute(
                        "INSERT INTO archive_sweep_read VALUES(?,?,?,?)",
                        (item["id"], read["slot"], read["at"], read["byte"]),
                    )
            touched.append(item["request_id"])
        for item in data.get("freezes", []):
            _validate_freeze(item)
            if record(db, "freeze", item):
                db.execute(
                    "INSERT INTO archive_freeze VALUES(?,?,?,?,?,?)",
                    (item["id"], item["request_id"], item.get("generation"), item["next_seq"], item["stop_receipt"], item["resume_receipt"]),
                )
                for read in item["reads"]:
                    db.execute(
                        "INSERT INTO archive_freeze_read VALUES(?,?,?,?)",
                        (item["id"], read["slot"], read["at"], read["byte"]),
                    )
            touched.append(item["request_id"])
        for item in data.get("mirrors", []):
            _validate_mirror(item)
            if record(db, "mirror", item):
                db.execute(
                    "INSERT INTO archive_mirror VALUES(?,?,?)",
                    (item["id"], item["request_id"], item.get("generation")),
                )
                for order, fragment in enumerate(item["fragments"]):
                    db.execute(
                        "INSERT INTO archive_mirror_fragment VALUES(?,?,?,?,?)",
                        (item["id"], fragment["seq"], fragment["byte"], fragment["receipt"], fragment["at"]),
                    )
            touched.append(item["request_id"])

        results = []
        for request_id in dict.fromkeys(touched):
            bundle = _load(db, request_id)
            if not bundle:
                continue
            verdict = _evaluate(
                bundle["request"],
                bundle["sweeps"],
                bundle["freezes"],
                bundle["mirrors"],
                bundle["restarts"],
            )
            scoped_ids = (
                {s["id"] for s in bundle["sweeps"]}
                | {f["id"] for f in bundle["freezes"]}
                | {m["id"] for m in bundle["mirrors"]}
                | {w["id"] for w in bundle["restarts"]}
            )
            # Reconcile the invalid ledger: a late restart witness can clear a
            # previous linkage failure, so stale rows must be removed first.
            for stale in db.execute("SELECT source_id,reason FROM archive_invalid").fetchall():
                if stale["source_id"] in scoped_ids:
                    db.execute(
                        "DELETE FROM archive_invalid WHERE source_id=? AND reason=?",
                        (stale["source_id"], stale["reason"]),
                    )
            for invalid in verdict["invalid_evidence"]:
                db.execute("INSERT OR IGNORE INTO archive_invalid VALUES(?,?)", (invalid["source"], invalid["reason"]))
            db.execute(
                """INSERT INTO archive_verdict VALUES(?,?,?)
                   ON CONFLICT(request_id) DO UPDATE SET state=excluded.state,verdict=excluded.verdict""",
                (request_id, verdict["state"], json.dumps(verdict, ensure_ascii=False, sort_keys=True)),
            )
            history(db, "archive-request", request_id, verdict["state"], "buffer-reader verdict")
            results.append(verdict)
    return results


def snapshot(db):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_verdict'"
    ).fetchone():
        return []
    return [
        json.loads(r["verdict"])
        for r in db.execute("SELECT verdict FROM archive_verdict ORDER BY request_id")
    ]
