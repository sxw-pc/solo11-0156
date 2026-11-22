"""Reconstruct whether two recorded loads really overlapped during a leg.

Only synthetic force values and local files are processed.  Nothing here is a
real transport liability finding, an equipment operation or a barcode
adjudication.

The rule is simple: two independent peak values are never added together.  A
combined overload is only signed at one global tick when concurrent samples
exist at that tick.

Two independent physical routes may testify.

``paired_runs``
    Two sensors recorded independent sync pulses in one continuous run, plus
    a trusted handover tick.  One clock offset is chosen for a whole run
    round; a different offset may not be picked per peak.  The raw series are
    then aligned and the crossing is narrowed to real ticks.

``witness_strip``
    One irreversible combined-force strip was installed at the start of a leg
    and recovered at its end.  With complete placement, recovery and leg
    start/end formalities it can sign that the leg crossed the limit, while
    the flip tick and every peak value remain unknown.

Missing samples are never zero-filled.  A calibration recorded after a
reboot is never used to back-fill an earlier run.  Candidate clock histories
are solved jointly: a peak that falls in the previous leg under one history
and in the next leg under another history cannot prove either leg.  When each
piece of old material is individually explainable but no single common
history exists, a conflict receipt is written and the conclusion stays
unverified.
"""

import itertools
import json

PAIRED = "paired_runs"
STRIP = "witness_strip"

POSITIVE = {"overload", "overload_phase"}
NEGATIVE = {"no_overload", "no_overload_phase"}
CONCLUSIVE = POSITIVE | NEGATIVE

SEVERE_ISSUES = {
    "no_common_history",
    "round_conflict",
    "sync_conflict",
    "sync_pin_conflict",
    "reboot_round_mismatch",
    "duplicate_device_round",
    "world_explosion",
}

CONTINUOUS_SAMPLING = {
    "continuous", "raw", "simultaneous", "paired", "realtime", "stream",
    "synchronous", "concurrent", "continuous_raw",
}


# ---------------------------------------------------------------------------
# Tolerant accessors and normalization
# ---------------------------------------------------------------------------

def _first(obj, *names, default=None):
    for name in names:
        if name in obj and obj[name] is not None:
            return obj[name]
    return default


def _as_int(value, what):
    if type(value) is bool or not isinstance(value, int):
        raise ValueError("invalid integer for " + what)
    return value


def _bool(value, default=False):
    return default if value is None else bool(value)


def _norm_leg(raw):
    leg_id = _first(raw, "id", "leg", "leg_id")
    if not leg_id:
        raise ValueError("transport leg lacks id")
    start = _as_int(_first(raw, "start_tick", "start", "from"), "leg start")
    end = _as_int(_first(raw, "end_tick", "end", "to"), "leg end")
    if end < start:
        raise ValueError("transport leg ends before it starts")
    return {"id": str(leg_id), "start": start, "end": end,
            "label": _first(raw, "label", "name", default=str(leg_id))}


def _norm_sample(raw):
    if isinstance(raw, (list, tuple)):
        if len(raw) < 2:
            raise ValueError("invalid sample")
        tick, value = raw[0], raw[1]
        valid = raw[2] if len(raw) > 2 else True
    else:
        tick = _first(raw, "tick", "t", "local_tick", "at")
        value = _first(raw, "value", "v", "force")
        valid = _first(raw, "valid", "ok", default=True)
    tick = _as_int(tick, "sample tick")
    if not _bool(valid, True) or value is None:
        return tick, None
    return tick, _as_int(value, "sample value")


def _norm_run(raw):
    run_id = _first(raw, "id", "run", "run_id")
    if not run_id:
        raise ValueError("run lacks id")
    device = str(_first(raw, "device", "sensor", "channel", default="")).upper()
    if not device:
        raise ValueError("run lacks device")
    table = {}
    contradictory = []
    for item in _first(raw, "samples", "series", "readings", default=[]):
        tick, value = _norm_sample(item)
        if tick in table:
            if table[tick] != value:
                contradictory.append(tick)
            table[tick] = None if table[tick] != value else value
        else:
            table[tick] = value
    offset = _first(raw, "offset", "clock_offset", "local_to_global")
    allowed = _first(raw, "allowed_offsets", "offsets", "candidate_offsets")
    if offset is not None:
        offset = _as_int(offset, "run offset")
    if allowed is not None:
        if not isinstance(allowed, list):
            raise ValueError("allowed_offsets must be a list")
        allowed = sorted({_as_int(x, "allowed offset") for x in allowed})
    return {
        "id": str(run_id),
        "device": device,
        "round": _first(raw, "round", "run_round", "sampling_round"),
        "offset": offset,
        "allowed_offsets": allowed,
        "samples": table,
        "contradictory_ticks": contradictory,
        "signed": _bool(_first(raw, "signed", "certified", "complete"), True),
        "source": raw,
    }


def _norm_sync(raw):
    sync_id = _first(raw, "id", "sync", "sync_id")
    if not sync_id:
        raise ValueError("sync anchor lacks id")
    pair = _first(raw, "runs", "pair")
    a_run = _first(raw, "a_run", "run_a", "left_run")
    b_run = _first(raw, "b_run", "run_b", "right_run")
    if isinstance(pair, (list, tuple)) and len(pair) == 2:
        a_run, b_run = pair
    if not a_run or not b_run:
        raise ValueError("sync anchor lacks two runs")
    a_tick = _first(raw, "a_tick", "tick_a", "left_tick")
    b_tick = _first(raw, "b_tick", "tick_b", "right_tick")
    offset = _first(raw, "offset", "delta", "clock_delta")
    tick = _first(raw, "tick", "global_tick", "handover_tick")
    return {
        "id": str(sync_id),
        "a_run": str(a_run),
        "b_run": str(b_run),
        "a_tick": None if a_tick is None else _as_int(a_tick, "sync a_tick"),
        "b_tick": None if b_tick is None else _as_int(b_tick, "sync b_tick"),
        "offset": None if offset is None else _as_int(offset, "sync offset"),
        "tick": None if tick is None else _as_int(tick, "sync tick"),
        "round": _first(raw, "round", "run_round", "sampling_round"),
        "source": raw,
    }


def _state(value):
    if value is None:
        return None
    value = str(value).lower()
    if value in ("flipped", "tripped", "changed", "overloaded", "crossed",
                 "triggered", "true", "yes"):
        return "flipped"
    if value in ("intact", "unchanged", "clear", "normal", "false", "no"):
        return "intact"
    return value


def _norm_strip(raw):
    strip_id = _first(raw, "id", "strip", "strip_id")
    if not strip_id:
        raise ValueError("witness strip lacks id")
    leg = _first(raw, "leg", "leg_id", "transport_leg")
    if not leg:
        raise ValueError("witness strip lacks leg")
    formalities = _first(raw, "formalities", "leg_formalities")
    if isinstance(formalities, dict):
        start_ok = _first(formalities, "start_ok", "start_complete",
                          "handover_start", default=False)
        end_ok = _first(formalities, "end_ok", "end_complete",
                        "handover_end", default=False)
    else:
        start_ok = _first(raw, "leg_start_ok", "start_ok", "start_complete",
                          default=False)
        end_ok = _first(raw, "leg_end_ok", "end_ok", "end_complete",
                        default=False)
    placed_tick = _first(raw, "placement_tick", "placed_tick", "install_tick")
    recovered_tick = _first(raw, "recovery_tick", "recovered_tick",
                            "retrieve_tick")
    return {
        "id": str(strip_id),
        "leg": str(leg),
        "placed_state": _state(_first(raw, "placed_state", "placement_state",
                                      "initial_state", "at_placement")),
        "recovered_state": _state(_first(raw, "recovered_state",
                                         "recovery_state", "final_state",
                                         "at_recovery")),
        "placement_complete": _bool(_first(
            raw, "placement_complete", "placed_complete", "placement_ok"), False),
        "recovery_complete": _bool(_first(
            raw, "recovery_complete", "recovered_complete", "recovery_ok"), False),
        "replacement": _bool(_first(raw, "replacement", "replaced", "swapped"),
                             False),
        "replacement_complete": _bool(_first(
            raw, "replacement_complete", "swap_complete",
            "replacement_record_complete", default=True), True),
        "placed_tick": None if placed_tick is None else _as_int(
            placed_tick, "strip placement tick"),
        "recovered_tick": None if recovered_tick is None else _as_int(
            recovered_tick, "strip recovery tick"),
        "start_ok": _bool(start_ok, False),
        "end_ok": _bool(end_ok, False),
        "round": _first(raw, "round", "run_round", "sampling_round"),
        "source": raw,
    }


def _claim(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "overload" if value else "no_overload"
    text = str(value).lower()
    if text in ("overload", "overloaded", "triggered", "flipped", "crossed",
                "true", "yes"):
        return "overload"
    if text in ("no_overload", "none", "normal", "intact", "false", "no"):
        return "no_overload"
    return "unknown"


def _norm_receipt(raw):
    receipt_id = _first(raw, "id", "receipt", "receipt_id")
    if not receipt_id:
        raise ValueError("handover receipt lacks id")
    tick = _first(raw, "tick", "claim_tick", "at", "incident_tick")
    return {
        "id": str(receipt_id),
        "leg": _first(raw, "leg", "leg_id", "transport_leg"),
        "tick": None if tick is None else _as_int(tick, "receipt tick"),
        "sampling": _first(raw, "sampling", "method", "sampling_method"),
        "round": _first(raw, "round", "run_round", "sampling_round"),
        "claims": _claim(_first(raw, "claims_overload", "overloaded", "claim",
                                "overload")),
        "source": raw,
    }


def normalize_case(raw):
    """Normalize one case document into a solver-friendly dictionary."""
    if not isinstance(raw, dict):
        raise ValueError("case document must be an object")
    case_id = _first(raw, "case", "case_id")
    document_id = _first(raw, "id", "doc_id", "document_id")
    if not case_id:
        case_id = document_id
    if not case_id:
        raise ValueError("case lacks id")
    limit = _first(raw, "limit", "overload_limit", "threshold", default=10)
    return {
        "id": str(case_id),
        "document_id": str(document_id or case_id),
        "limit": _as_int(limit, "limit"),
        "legs": [_norm_leg(x) for x in _first(raw, "legs", "transport_legs",
                                               default=[])],
        "runs": [_norm_run(x) for x in _first(raw, "runs", "channels",
                                               "sensors", default=[])],
        "syncs": [_norm_sync(x) for x in _first(raw, "syncs", "anchors",
                                                "pulses", default=[])],
        "strips": [_norm_strip(x) for x in _first(raw, "strips",
                                                  "witness_strips",
                                                  "witnesses", default=[])],
        "receipts": [_norm_receipt(x) for x in _first(raw, "receipts",
                                                      "handover_receipts",
                                                      default=[])],
        "source": raw,
    }


# ---------------------------------------------------------------------------
# Clock alignment: one offset per run, never one per peak
# ---------------------------------------------------------------------------

def _run_domain(run):
    if run["offset"] is not None:
        if run["allowed_offsets"] is not None \
                and run["offset"] not in run["allowed_offsets"]:
            return []
        return [run["offset"]]
    if run["allowed_offsets"] is not None:
        return list(run["allowed_offsets"])
    return []


def _sync_delta(sync):
    """Return ``(off_b - off_a, problem)`` proven by one sync pulse."""
    derived = None
    if sync["a_tick"] is not None and sync["b_tick"] is not None:
        derived = sync["a_tick"] - sync["b_tick"]
    if sync["offset"] is not None:
        if derived is not None and derived != sync["offset"]:
            return None, "sync offset contradicts its two local pulse ticks"
        return sync["offset"], None
    if derived is not None:
        return derived, None
    return None, "sync pulse has neither two local ticks nor a recorded difference"


def _issue(kind, detail, evidence, leg=None):
    return {"kind": kind, "leg": leg, "detail": detail,
            "evidence": list(evidence)}


def build_alignment(case):
    """Group runs by proven sync edges and enumerate offsets per run.

    Each assignment fixes exactly one clock offset for every run in a
    continuous round.  Fitting a separate offset to each peak is therefore
    impossible.
    """
    runs = {r["id"]: r for r in case["runs"]}
    issues = []
    adjacency = {rid: [] for rid in runs}
    syncs = []

    for sync in case["syncs"]:
        a, b = sync["a_run"], sync["b_run"]
        if a not in runs or b not in runs:
            issues.append(_issue("sync_unknown_run",
                                 "sync %s names an unknown run" % sync["id"],
                                 [sync["id"]]))
            continue
        ra, rb = runs[a]["round"], runs[b]["round"]
        rounds = {x for x in (ra, rb, sync["round"]) if x is not None}
        if len(rounds) > 1:
            issues.append(_issue(
                "reboot_round_mismatch",
                "sync %s ties different sampling rounds %s" % (sync["id"], sorted(rounds)),
                [sync["id"], a, b]))
            continue
        delta, problem = _sync_delta(sync)
        if problem:
            issues.append(_issue("sync_untimed", problem, [sync["id"], a, b]))
            syncs.append(sync)
            continue
        adjacency[a].append((b, delta, sync))
        adjacency[b].append((a, -delta, sync))
        syncs.append(sync)

    group_of = {}
    groups = []
    for rid in sorted(runs):
        if rid in group_of:
            continue
        group_id = len(groups)
        group_of[rid] = group_id
        stack = [rid]
        members = []
        while stack:
            current = stack.pop()
            members.append(current)
            for other, _delta, _sync in adjacency[current]:
                if other not in group_of:
                    group_of[other] = group_id
                    stack.append(other)
        groups.append({"id": group_id, "members": sorted(members),
                       "assignments": [], "invalid": [], "round": None,
                       "sync_pinned": False})

    for group in groups:
        members = set(group["members"])
        known_rounds = {runs[x]["round"] for x in members
                        if runs[x]["round"] is not None}
        sync_rounds = {s["round"] for s in syncs
                       if s["round"] is not None
                       and s["a_run"] in members and s["b_run"] in members}
        if len(known_rounds | sync_rounds) > 1:
            group["invalid"].append("more than one sampling round inside one run")
            issues.append(_issue("round_conflict",
                                 "run has conflicting sampling rounds",
                                 sorted(members)))
            continue
        group["round"] = next(iter(known_rounds | sync_rounds), None)

        devices = {}
        for rid in members:
            devices.setdefault(runs[rid]["device"], []).append(rid)
        duplicates = [ids for ids in devices.values() if len(ids) > 1]
        if duplicates:
            group["invalid"].append("two runs of one device in one continuous run")
            issues.append(_issue(
                "duplicate_device_round",
                "one device has two simultaneous runs in one round",
                sorted(x for ids in duplicates for x in ids)))
            continue

        root = sorted(members)[0]
        relative = {root: 0}
        queue = [root]
        consistent = True
        while queue and consistent:
            current = queue.pop()
            for other, delta, sync in adjacency[current]:
                if other not in relative:
                    relative[other] = relative[current] + delta
                    queue.append(other)
                elif relative[other] != relative[current] + delta:
                    consistent = False
                    issues.append(_issue(
                        "sync_conflict",
                        "sync mesh gives two clock differences for %s" % other,
                        [sync["id"], current, other]))
                    break
        if not consistent:
            group["invalid"].append("clock difference mesh is contradictory")
            continue

        pinned_bases = set()
        for sync in syncs:
            if sync["tick"] is None:
                continue
            if sync["a_run"] in members and sync["a_tick"] is not None:
                pinned_bases.add(sync["tick"] - sync["a_tick"]
                                 - relative[sync["a_run"]])
            if sync["b_run"] in members and sync["b_tick"] is not None:
                pinned_bases.add(sync["tick"] - sync["b_tick"]
                                 - relative[sync["b_run"]])
        declared_bases = {runs[rid]["offset"] - relative[rid]
                          for rid in members
                          if runs[rid]["offset"] is not None}
        all_bases = pinned_bases | declared_bases
        if len(all_bases) > 1:
            group["invalid"].append("absolute handover ticks disagree")
            issues.append(_issue(
                "sync_pin_conflict",
                "two absolute pins imply different clock bases %s"
                % sorted(all_bases), sorted(members)))
            continue

        if all_bases:
            bases = sorted(all_bases)
        else:
            domains = [_run_domain(runs[rid]) for rid in group["members"]]
            if any(not domain for domain in domains):
                group["invalid"].append("clock offset is not documented")
                issues.append(_issue(
                    "offset_unknown",
                    "run has neither a sync pin nor allowed offsets",
                    sorted(members)))
                continue
            bases = []
            for combo in itertools.product(*domains):
                base = combo[0] - relative[group["members"][0]]
                if all(base + relative[rid] == combo[i]
                       for i, rid in enumerate(group["members"])):
                    bases.append(base)
            bases = sorted(set(bases))

        assignments = []
        seen = set()
        for base in bases:
            offsets = {rid: base + relative[rid] for rid in members}
            if any(runs[rid]["offset"] is not None
                   and offsets[rid] != runs[rid]["offset"] for rid in members):
                continue
            if any(runs[rid]["allowed_offsets"] is not None
                   and offsets[rid] not in runs[rid]["allowed_offsets"]
                   for rid in members):
                continue
            if not _sync_ticks_match(syncs, members, offsets):
                continue
            key = tuple(sorted(offsets.items()))
            if key not in seen:
                seen.add(key)
                assignments.append(offsets)
        group["assignments"] = assignments
        group["sync_pinned"] = bool(pinned_bases)
        if not assignments:
            group["invalid"].append(
                "no clock assignment satisfies the declared offsets")
            issues.append(_issue(
                "no_common_history",
                "each piece fits alone but no clock assignment fits all",
                sorted(members)))

    by_device_round = {}
    for run in case["runs"]:
        if run["round"] is not None:
            by_device_round.setdefault((run["device"], run["round"]),
                                       []).append(run["id"])
    for (device, round_id), ids in by_device_round.items():
        if len(ids) > 1:
            issues.append(_issue(
                "duplicate_device_round",
                "%s has two runs in round %s" % (device, round_id),
                sorted(ids)))

    return {"runs": runs, "groups": groups, "group_of": group_of,
            "issues": issues,
            "severe": [x for x in issues if x["kind"] in SEVERE_ISSUES]}


def _sync_ticks_match(syncs, members, offsets):
    for sync in syncs:
        a, b = sync["a_run"], sync["b_run"]
        if a not in members or b not in members:
            continue
        if sync["tick"] is None:
            continue
        if sync["a_tick"] is not None and sync["tick"] != sync["a_tick"] + offsets[a]:
            return False
        if sync["b_tick"] is not None and sync["tick"] != sync["b_tick"] + offsets[b]:
            return False
    return True


def build_worlds(case, alignment):
    """Enumerate joint clock histories.

    A world fixes one offset for every run.  The whole document is evaluated
    world by world, so evidence from different worlds is never combined.
    """
    if alignment["severe"]:
        return [], [dict(x) for x in alignment["issues"]]
    groups = alignment["groups"]
    choices = [g["assignments"] if g["assignments"] else []
               for g in groups]
    if any(not choice for choice in choices):
        return [], [dict(x) for x in alignment["issues"]]
    total = 1
    for choice in choices:
        total *= len(choice)
    if total > 256:
        issue = _issue("world_explosion",
                       "too many candidate clock histories", [])
        return [], [dict(x) for x in alignment["issues"]] + [issue]
    worlds = []
    for combo in itertools.product(*choices):
        offsets = {}
        for assignment in combo:
            offsets.update(assignment)
        worlds.append({
            "offsets": offsets,
            "group_of": alignment["group_of"],
            "groups": alignment["groups"],
        })
    return worlds, [dict(x) for x in alignment["issues"]]


# ---------------------------------------------------------------------------
# Concurrent samples in one world
# ---------------------------------------------------------------------------

def _sample(run, global_tick, offset):
    return run["samples"].get(global_tick - offset)


def _evaluate_pair(case, world, leg, run_a, run_b, group_a, group_b):
    same_group = group_a["id"] == group_b["id"]
    if same_group:
        sync_validated = bool(group_a["sync_pinned"])
        round_known = group_a["round"] is not None
    else:
        if group_a["round"] is None or group_b["round"] is None:
            return None, "unknown_round"
        if group_a["round"] != group_b["round"]:
            return None, "round_mismatch"
        sync_validated = False
        round_known = True
    offsets = world["offsets"]
    overload = []
    missing = []
    covered = []
    for tick in range(leg["start"], leg["end"] + 1):
        value_a = _sample(run_a, tick, offsets[run_a["id"]])
        value_b = _sample(run_b, tick, offsets[run_b["id"]])
        if value_a is None or value_b is None:
            missing.append(tick)
            continue
        covered.append(tick)
        total = value_a + value_b
        if total >= case["limit"]:
            overload.append({
                "tick": tick,
                "sum": total,
                "values": {run_a["device"]: value_a,
                           run_b["device"]: value_b},
            })
    return {
        "pair": [run_a["id"], run_b["id"]],
        "devices": [run_a["device"], run_b["device"]],
        "offsets": {run_a["id"]: offsets[run_a["id"]],
                    run_b["id"]: offsets[run_b["id"]]},
        "round": group_a["round"],
        "round_known": round_known,
        "sync_validated": sync_validated,
        "signed": run_a["signed"] and run_b["signed"],
        "overload": overload,
        "covered": covered,
        "missing": missing,
        "complete": not missing,
    }, None


def _run_pairs(case):
    runs = case["runs"]
    for index, run_a in enumerate(runs):
        for run_b in runs[index + 1:]:
            if run_a["device"] != run_b["device"]:
                yield run_a, run_b


def _world_leg_outcome(case, alignment, world, leg):
    """Best joint pair outcome for one leg in one world.

    Pairs from different sampling rounds are separate histories: they are
    never merged and never compared against each other.  Inside one round the
    scope is two synthetic devices, so one cross-device pair is expected; if
    several same-round pairs exist their overload tick sets must agree.
    """
    outcomes = []
    skips = []
    for run_a, run_b in _run_pairs(case):
        group_a = alignment["groups"][alignment["group_of"][run_a["id"]]]
        group_b = alignment["groups"][alignment["group_of"][run_b["id"]]]
        outcome, reason = _evaluate_pair(case, world, leg, run_a, run_b,
                                         group_a, group_b)
        if reason:
            skips.append({"pair": [run_a["id"], run_b["id"]],
                          "reason": reason})
        else:
            outcomes.append(outcome)
    if not outcomes:
        return None, skips

    by_round = {}
    for outcome in outcomes:
        by_round.setdefault(str(outcome["round"]), []).append(outcome)
    histories = []
    for round_key in sorted(by_round):
        group = by_round[round_key]
        tick_sets = [set(item["tick"] for item in outcome["overload"])
                     for outcome in group]
        common = set.intersection(*tick_sets)
        possible = set.union(*tick_sets)
        contradictory = len(tick_sets) > 1 and common != possible
        item_by_tick = {}
        merged_offsets = {}
        for outcome in group:
            for item in outcome["overload"]:
                item_by_tick.setdefault(item["tick"], item)
            merged_offsets.update(outcome["offsets"])
        histories.append({
            "contradictory_pairs": contradictory,
            "overload_ticks": sorted(common) if not contradictory else [],
            "possible_ticks": sorted(possible),
            "missing": sorted({t for o in group for t in o["missing"]}),
            "complete": all(o["complete"] for o in group),
            "signed": all(o["signed"] for o in group),
            "sync_validated": all(o["sync_validated"] for o in group),
            "round": group[0]["round"],
            "round_known": all(o["round_known"] for o in group),
            "pairs": [o["pair"] for o in group],
            "items": item_by_tick,
            "offsets": merged_offsets,
        })
    return {"histories": histories}, skips


def _signature(outcome):
    return (
        tuple(outcome["overload_ticks"]),
        tuple((item["tick"], item["sum"])
              for item in sorted(outcome["items"].values(),
                                 key=lambda x: x["tick"])),
        tuple(sorted(outcome["offsets"].items())),
        tuple(outcome["missing"]),
        outcome["complete"],
        outcome["signed"],
        outcome["sync_validated"],
        str(outcome["round"]),
        outcome["contradictory_pairs"],
    )


def evaluate_paired(case, alignment, worlds, leg):
    """All joint histories for one leg.

    A hypothesis is one whole-world, one-round explanation.  Nothing is
    combined across worlds or across sampling rounds.
    """
    if not case["runs"]:
        return {
            "status": "unverified", "tick_known": False, "ticks": [],
            "overload_ticks": [], "possible_ticks": [], "common_ticks": [],
            "missing_ticks": [], "missing_samples": [],
            "contradictory_samples": [], "hypotheses": [],
            "sync_validated": False, "basis": None, "skipped": [],
            "missing_observations": [
                "two independent device series from one continuous run"],
        }
    outcomes = []
    skipped = []
    for world in worlds:
        outcome, skips = _world_leg_outcome(case, alignment, world, leg)
        skipped.extend(skips)
        if outcome is not None:
            outcomes.extend(outcome["histories"])

    unique = {}
    for outcome in outcomes:
        unique.setdefault(_signature(outcome), outcome)
    unique = list(unique.values())

    hypotheses = [{
        "pairs": outcome["pairs"],
        "offsets": outcome["offsets"],
        "round": outcome["round"],
        "round_known": outcome.get("round_known", False),
        "sync_validated": outcome["sync_validated"],
        "complete": outcome["complete"],
        "signed": outcome["signed"],
        "overload_ticks": outcome["overload_ticks"],
        "possible_ticks": outcome["possible_ticks"],
        "sums": {str(item["tick"]): item["sum"]
                 for item in outcome["items"].values()},
        "missing_ticks": outcome["missing"],
    } for outcome in unique]

    base = {
        "status": "unverified",
        "tick_known": False,
        "ticks": [],
        "overload_ticks": [],
        "possible_ticks": [],
        "common_ticks": [],
        "missing_ticks": [],
        "missing_samples": [],
        "contradictory_samples": [],
        "hypotheses": hypotheses,
        "sync_validated": False,
        "basis": None,
        "skipped": skipped,
        "missing_observations": [],
    }

    if not unique:
        reasons = {item["reason"] for item in skipped}
        if reasons == {"round_mismatch"}:
            # The runs belong to different sampling rounds and no sync ties
            # them.  That is missing concurrency evidence, not a contradiction:
            # a later calibration must not be used to back-date the old run.
            base["status"] = "unverified"
            base["missing_observations"] = [
                "two channels recorded in one common sampling round; a "
                "calibration recorded after a reboot cannot back-fill an "
                "earlier run",
                "the incident tick and the sampling round must be solved "
                "together, not chosen per peak",
            ]
        elif "unknown_round" in reasons:
            base["status"] = "unverified"
            base["missing_observations"] = [
                "the sampling round for both channels",
                "a sync pulse tying the two runs to one continuous round",
            ]
        else:
            base["missing_observations"] = [
                "two independent device series from one continuous run",
            ]
        return base

    all_sync = all(x["sync_validated"] for x in unique)
    all_complete = all(x["complete"] for x in unique)
    all_signed = all(x["signed"] for x in unique)
    any_contradictory = any(x["contradictory_pairs"] for x in unique)
    positive = [x for x in unique if x["overload_ticks"]]
    negative = [x for x in unique if not x["overload_ticks"]]
    unknown = [x for x in unique if not x["overload_ticks"] and not x["complete"]]
    negative_complete = [x for x in negative if x["complete"]]

    tick_sets = [set(x["overload_ticks"]) for x in unique]
    common_ticks = set.intersection(*tick_sets)
    possible_ticks = set.union(*(set(x["possible_ticks"]) for x in unique))
    missing_ticks = sorted({t for x in unique for t in x["missing"]})
    item_by_tick = {}
    for outcome in unique:
        for item in outcome["items"].values():
            item_by_tick.setdefault(item["tick"], item)

    base["possible_ticks"] = sorted(possible_ticks)
    base["common_ticks"] = sorted(common_ticks) if all_sync else []
    base["missing_ticks"] = missing_ticks
    base["sync_validated"] = all_sync
    base["missing_samples"] = _missing_samples(unique, leg, case)
    base["contradictory_samples"] = _contradictory_samples(case, leg, unique)
    if base["contradictory_samples"]:
        base["missing_observations"].append(
            "reconciled readings at global ticks %s; contradictory duplicates "
            "are kept unobserved rather than averaged or replaced"
            % sorted({x["tick"] for x in base["contradictory_samples"]}))

    if any_contradictory:
        base["status"] = "conflict"
        base["missing_observations"] = [
            "cross-device pairs disagree on the overloaded ticks",
            "a single common history for all recorded sensors",
        ]
        return base

    if len(positive) == len(unique):
        if not all_sync:
            base["status"] = "unverified"
            base["missing_observations"] = [
                "a common-run sync pulse and a trusted handover tick",
                "concurrent raw readings at one proven global tick",
            ]
            return base
        if not all_signed:
            base["status"] = "unverified"
            base["missing_observations"] = [
                "a signed completeness window for both channels",
            ]
            return base
        distinct_tick_sets = {tuple(x["overload_ticks"]) for x in unique}
        if common_ticks:
            base["status"] = "overload"
            base["tick_known"] = True
            base["ticks"] = [item_by_tick[t] for t in sorted(common_ticks)]
            base["overload_ticks"] = sorted(common_ticks)
            base["basis"] = PAIRED
            if missing_ticks:
                base["missing_observations"].append(
                    "valid samples at global ticks %s; those ticks remain "
                    "unobserved rather than zero" % missing_ticks)
            if possible_ticks - common_ticks:
                base["missing_observations"].append(
                    "one common history for the additional candidate ticks %s"
                    % sorted(possible_ticks - common_ticks))
            return base
        if len(distinct_tick_sets) > 1:
            # Every joint history says the leg crossed, but at a different
            # tick under each history.  The phase is therefore signed across
            # histories while the specific tick is not: a peak from one
            # history must not be cross-cited with a peak from another.
            base["status"] = "overload_phase"
            base["tick_known"] = False
            base["basis"] = PAIRED
            base["missing_observations"] = [
                "one common clock history that pins the crossing to a tick; "
                "the candidate ticks are %s" % sorted(possible_ticks),
                "a flip tick; it must not be borrowed from another history",
            ]
            return base
        # The same tick set is produced under more than one whole-run offset,
        # but the crossing is not shared.  Only ambiguity remains.
        base["status"] = "unverified"
        base["missing_observations"] = [
            "one documented per-run clock offset; the candidate offset sets "
            "are %s" % [x["offsets"] for x in unique],
            "the crossing tick %s is not signed until one offset serves the "
            "whole run" % sorted(possible_ticks),
        ]
        return base

    if not positive and negative_complete and len(negative_complete) == len(unique):
        if all_sync and all_signed:
            base["status"] = "no_overload"
            base["tick_known"] = True
            base["basis"] = PAIRED
            return base
        base["status"] = "unverified"
        base["missing_observations"] = [
            "a common-run sync pulse and a trusted handover tick",
            "a signed complete window for both channels",
        ]
        return base

    if negative and not positive:
        base["status"] = "insufficient_coverage" if unknown else "unverified"
        base["missing_observations"] = [
            "valid (never zero-filled) samples at global ticks %s for both "
            "channels" % missing_ticks,
        ]
        if not all_sync:
            base["missing_observations"].append(
                "a common-run sync pulse and a trusted handover tick")
        return base

    if not all_sync:
        # Without one proven common round the per-run clock is unresolved.
        # If a complete history says the leg did not cross, the crossing can
        # only be produced by a different history: no single history wins.
        negative_evidence = [x for x in unique if x["complete"] and
                             x["sync_validated"]]
        if negative_evidence:
            base["status"] = "conflict"
            base["missing_observations"] = [
                "a common-run sync pulse and a trusted handover tick",
                "one common history covering both channels; candidate ticks "
                "%s come from different per-run offsets and must not be "
                "cross-cited" % sorted(possible_ticks),
            ]
            return base
        base["status"] = "unverified"
        base["missing_observations"] = [
            "a common-run sync pulse and a trusted handover tick; candidate "
            "ticks %s must not be cross-cited across histories"
            % sorted(possible_ticks),
        ]
        return base

    # Some histories say the leg crossed, others do not, although the round
    # itself is proven.  Each local peak is explainable on its own, but there
    # is no single history for the whole round: a conflict receipt with an
    # unverified conclusion.  Candidate ticks from different histories are
    # never cross-cited.
    base["status"] = "conflict"
    base["missing_observations"] = [
        "one common history covering both channels; a peak placed in one leg "
        "under one history cannot be cross-cited with a peak placed under "
        "another history",
        "a joint solution for the incident tick and the sampling round",
    ]
    return base


def _missing_samples(outcomes, leg, case):
    """Attribute each unobserved leg tick to the channels that lack a sample."""
    runs = {run["id"]: run for run in case["runs"]}
    devices = {}
    for outcome in outcomes:
        missing = set(outcome["missing"])
        for pair in outcome["pairs"]:
            for run_id in pair:
                run = runs[run_id]
                for tick in missing:
                    if _sample(run, tick, outcome["offsets"][run_id]) is None:
                        devices.setdefault(tick, set()).add(run["device"])
    return [{"leg": leg["id"], "tick": tick,
             "devices": sorted(devices[tick])}
            for tick in sorted(devices)]


def _contradictory_samples(case, leg, outcomes):
    """Report duplicated local ticks that disagreed inside one run.

    A run records one local tick once.  Two different values at the same local
    tick are contradictory originals: the tick is kept unobserved, not
    averaged, and the conflict is surfaced for an operator to review.
    """
    runs = {run["id"]: run for run in case["runs"]}
    found = []
    seen = set()
    for outcome in outcomes:
        for pair in outcome["pairs"]:
            for run_id in pair:
                run = runs[run_id]
                offset = outcome["offsets"][run_id]
                for local_tick in run["contradictory_ticks"]:
                    global_tick = local_tick + offset
                    if not (leg["start"] <= global_tick <= leg["end"]):
                        continue
                    key = (run["device"], global_tick)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append({"leg": leg["id"], "tick": global_tick,
                                  "device": run["device"],
                                  "local_tick": local_tick,
                                  "run": run_id})
    return sorted(found, key=lambda x: (x["tick"], x["device"]))



# ---------------------------------------------------------------------------
# Irreversible witness strip
# ---------------------------------------------------------------------------

def _strip_reasons(strip, leg):
    reasons = []
    if not strip["placement_complete"]:
        reasons.append("placement record is incomplete")
    if not strip["recovery_complete"]:
        reasons.append("recovery record is incomplete")
    if strip["replacement"] and not strip["replacement_complete"]:
        reasons.append("replacement record is incomplete")
    if not strip["start_ok"]:
        reasons.append("leg start formality is not proven")
    if not strip["end_ok"]:
        reasons.append("leg end formality is not proven")
    if strip["placed_state"] is None:
        reasons.append("placement state is unknown")
    elif strip["placed_state"] != "intact":
        reasons.append("strip was already changed at placement")
    if strip["recovered_state"] not in ("flipped", "intact"):
        reasons.append("recovery state is unknown")
    placed = strip["placed_tick"]
    recovered = strip["recovered_tick"]
    if placed is not None or recovered is not None:
        if placed is None or recovered is None:
            reasons.append("strip span is only partly recorded")
        elif not (leg["start"] <= placed <= recovered <= leg["end"]):
            reasons.append("strip span lies outside this leg")
    return reasons


def evaluate_strips(case, leg):
    usable = []
    unusable = []
    for strip in case["strips"]:
        if strip["leg"] != leg["id"]:
            continue
        reasons = _strip_reasons(strip, leg)
        if strip["round"] is not None:
            case_rounds = {run["round"] for run in case["runs"]
                           if run["round"] is not None}
            if case_rounds and strip["round"] not in case_rounds:
                reasons.append("strip round is not documented in this case")
        if reasons:
            unusable.append({"id": strip["id"], "reason": "; ".join(reasons)})
        else:
            usable.append(strip)

    flipped = [s for s in usable if s["recovered_state"] == "flipped"]
    intact = [s for s in usable if s["recovered_state"] == "intact"]
    result = {
        "usable": [s["id"] for s in usable],
        "unusable": unusable,
        "status": "unusable",
        "tick_known": False,
        "missing_observations": [],
    }
    if flipped and intact:
        result["status"] = "conflict"
        result["missing_observations"] = [
            "consistent recovery states for the usable witness strips"]
        return result
    if flipped:
        result["status"] = "overload"
        result["missing_observations"] = [
            "the strip signs the leg phase only; the flip tick and every peak "
            "value remain unknown",
        ]
    elif intact:
        result["status"] = "no_overload"
    else:
        if not any(s["leg"] == leg["id"] for s in case["strips"]):
            result["missing_observations"].append(
                "no witness strip placed for this leg")
        for item in unusable:
            result["missing_observations"].append(
                "usable witness strip: " + item["reason"])
    return result


# ---------------------------------------------------------------------------
# Handover receipts
# ---------------------------------------------------------------------------

def _receipt_leg(case, receipt):
    """Resolve the phase (leg) of a receipt.

    A tick that falls in a leg is trusted as the incident tick; when the leg
    name is absent, the phase is derived from that same tick.  When both the
    tick and the round are unknown, they are left for joint solving instead
    of being guessed.
    """
    by_tick = [leg for leg in case["legs"]
               if receipt["tick"] is not None
               and leg["start"] <= receipt["tick"] <= leg["end"]]
    if receipt["leg"] is not None:
        declared = next((leg for leg in case["legs"]
                         if leg["id"] == str(receipt["leg"])), None)
        if declared is None:
            return None, _issue(
                "receipt_unknown_leg",
                "receipt %s names a leg not present in the case"
                % receipt["id"], [receipt["id"]], str(receipt["leg"]))
        if receipt["tick"] is not None and not (
                declared["start"] <= receipt["tick"] <= declared["end"]):
            return declared["id"], _issue(
                "receipt_phase_conflict",
                "receipt %s tick %s lies outside leg %s"
                % (receipt["id"], receipt["tick"], declared["id"]),
                [receipt["id"]], declared["id"])
        return declared["id"], None
    if len(by_tick) == 1:
        return by_tick[0]["id"], None
    if len(by_tick) > 1:
        return None, _issue(
            "receipt_phase_ambiguous",
            "receipt %s tick falls in more than one leg" % receipt["id"],
            [receipt["id"]], None)
    if receipt["tick"] is None:
        return None, _issue(
            "receipt_untimed",
            "receipt %s has neither a leg nor an incident tick" % receipt["id"],
            [receipt["id"]], None)
    return None, _issue(
        "receipt_tick_out_of_scope",
        "receipt %s tick is outside every declared leg" % receipt["id"],
        [receipt["id"]], None)


def evaluate_receipts(case, leg, paired):
    """Corroborate or contradict through an independent original receipt.

    A receipt can be trusted for the incident tick, the transport phase and
    the sampling method it recorded.  It never reconstructs concurrent force
    and never supplies a peak value.
    """
    entries = []
    conflicts = []
    missing = []
    for receipt in case["receipts"]:
        leg_id, issue = _receipt_leg(case, receipt)
        if issue is not None:
            if issue["leg"] is not None and issue["leg"] != leg["id"]:
                # This receipt belongs to another leg's disposition.
                continue
            if issue["kind"] == "receipt_phase_conflict":
                conflicts.append(issue)
            else:
                missing.append(
                    "a receipt that can be placed: %s (phase and sampling "
                    "round must be solved together, not assumed from a route "
                    "name)" % issue["detail"])
            continue
        if leg_id != leg["id"]:
            continue
        sampling = (None if receipt["sampling"] is None
                    else str(receipt["sampling"]).lower())
        sampling_ok = sampling in CONTINUOUS_SAMPLING
        entries.append({
            "id": receipt["id"],
            "tick": receipt["tick"],
            "claim": receipt["claims"],
            "leg_trusted": True,
            "tick_trusted": receipt["tick"] is not None,
            "sampling": receipt["sampling"],
            "sampling_preserves_concurrency": sampling_ok,
            "round": receipt["round"],
        })
        if receipt["tick"] is None:
            missing.append(
                "the incident tick of receipt %s; the phase cannot be fixed "
                "without it" % receipt["id"])
        if not sampling_ok:
            missing.append(
                "a sampling method that preserves concurrent raw readings "
                "(receipt %s recorded %s)"
                % (receipt["id"], receipt["sampling"]))
        if receipt["claims"] == "overload":
            if sampling_ok and paired["status"] in ("overload", "no_overload"):
                if paired["status"] == "no_overload":
                    conflicts.append(_issue(
                        "receipt_paired_conflict",
                        "paired samples say no overload while receipt %s "
                        "claims overload" % receipt["id"],
                        [receipt["id"]], leg["id"]))
                elif receipt["tick"] is not None and \
                        receipt["tick"] not in paired["overload_ticks"]:
                    conflicts.append(_issue(
                        "receipt_tick_conflict",
                        "receipt %s points at tick %s but paired overload "
                        "ticks are %s" % (receipt["id"], receipt["tick"],
                                          paired["overload_ticks"]),
                        [receipt["id"]], leg["id"]))
            elif paired["status"] in ("overload", "no_overload"):
                # A lossy summary cannot contradict a concurrent raw series.
                missing.append(
                    "a concurrent sampling record for receipt %s; its %s "
                    "summary cannot be compared tick by tick"
                    % (receipt["id"], receipt["sampling"]))
            else:
                missing.append(
                    "concurrent raw readings; receipt %s alone only places "
                    "the phase and the incident tick" % receipt["id"])
        elif receipt["claims"] == "no_overload":
            if paired["status"] in ("overload", "overload_phase") and sampling_ok:
                conflicts.append(_issue(
                    "receipt_paired_conflict",
                    "paired evidence says overload while receipt %s claims "
                    "none" % receipt["id"], [receipt["id"]], leg["id"]))
        elif receipt["claims"] == "unknown":
            missing.append("a readable overload claim in receipt %s"
                           % receipt["id"])
    return entries, conflicts, _dedupe(missing)


# ---------------------------------------------------------------------------
# Combine the two physical routes and receipts
# ---------------------------------------------------------------------------

def _combine_leg(case, leg, paired, strip, receipts, receipt_conflicts):
    conflicts = list(receipt_conflicts)
    missing = list(paired["missing_observations"])
    strip_missing = list(strip["missing_observations"])

    status = paired["status"]
    tick_known = paired["tick_known"]
    ticks = list(paired["ticks"])
    basis = paired["basis"]

    paired_positive = paired["status"] == "overload"
    paired_negative = paired["status"] == "no_overload"
    paired_phase = paired["status"] == "overload_phase"
    strip_positive = strip["status"] == "overload"
    strip_negative = strip["status"] == "no_overload"

    if strip["status"] == "conflict":
        conflicts.append(_issue(
            "strip_conflict", "usable witness strips disagree",
            strip["usable"], leg["id"]))
    elif strip_positive and paired["status"] in ("no_overload",
                                                 "no_overload_phase"):
        conflicts.append(_issue(
            "paired_strip_conflict",
            "paired samples say no overload while the witness strip flipped",
            strip["usable"] + [leg["id"]], leg["id"]))
    elif strip_negative and paired["status"] in ("overload", "overload_phase"):
        conflicts.append(_issue(
            "paired_strip_conflict",
            "paired samples say overload while the witness strip stayed intact",
            strip["usable"] + [leg["id"]], leg["id"]))
    elif strip_positive and paired_positive:
        status = "overload"
        tick_known = True
        ticks = paired["ticks"]
        basis = "paired_runs+witness_strip"
    elif strip_negative and paired_negative:
        status = "no_overload"
        tick_known = False
        ticks = []
        basis = "paired_runs+witness_strip"
    elif strip_positive:
        # The strip may sign the phase even when the common clock was lost,
        # but it never supplies the flip tick or a peak value.  Candidates
        # from different histories are not promoted into a tick.
        status = "overload_phase"
        tick_known = False
        ticks = []
        basis = STRIP
        missing.extend(strip_missing)
        if paired_phase:
            missing.append(
                "one common clock history; paired candidate ticks %s must "
                "not be combined across histories" % paired["possible_ticks"])
        elif paired["hypotheses"]:
            missing.append(
                "one common history for the paired candidates; they are "
                "recorded, not signed")
    elif strip_negative:
        status = "no_overload_phase"
        tick_known = False
        ticks = []
        basis = STRIP
        missing.extend(strip_missing)
    elif strip["unusable"]:
        missing.extend(strip_missing)

    if paired["status"] == "conflict" and strip["status"] in (
            "overload", "no_overload"):
        conflicts.append(_issue(
            "no_common_history",
            "paired material alone and the strip alone each explain one "
            "history, but no single common history exists",
            [case["document_id"], leg["id"]] + strip["usable"], leg["id"]))

    if conflicts:
        status = "conflict"
        tick_known = False
        ticks = []
        basis = None
        missing.append(
            "two independent originals contradict each other; both are kept "
            "and neither route is preferred")
    elif paired["status"] == "conflict":
        status = "conflict"
        tick_known = False
        ticks = []
        basis = None
        missing.append(
            "conflicting histories are kept as evidence; no route is "
            "preferred and the conclusion stays unverified")

    return {
        "leg": leg["id"],
        "start": leg["start"],
        "end": leg["end"],
        "status": status,
        "tick_known": bool(tick_known and status == "overload"),
        "ticks": ticks,
        "overload_ticks": sorted({item["tick"] for item in ticks}),
        "possible_ticks": paired["possible_ticks"],
        "common_ticks": paired["common_ticks"],
        "missing_ticks": paired["missing_ticks"],
        "missing_samples": paired["missing_samples"],
        "contradictory_samples": paired.get("contradictory_samples", []),
        "basis": basis,
        "sync_validated": paired["sync_validated"],
        "hypotheses": paired["hypotheses"],
        "paired": {key: paired[key] for key in
                   ("status", "tick_known", "ticks", "overload_ticks",
                    "possible_ticks", "common_ticks", "missing_ticks",
                    "missing_samples", "contradictory_samples",
                    "sync_validated", "skipped")},
        "strip": strip,
        "receipts": receipts,
        "missing_observations": _dedupe(missing),
    }, conflicts


def _positive_worlds(case, alignment, worlds):
    """For each leg, the world indices under which that leg would cross.

    A world fixes one clock offset for every run, so a peak cannot be counted
    in the previous leg under one history and in the next leg under another.
    """
    result = {}
    for leg in case["legs"]:
        indices = set()
        for index, world in enumerate(worlds):
            outcome, _skips = _world_leg_outcome(case, alignment, world, leg)
            if outcome is None:
                continue
            for history in outcome["histories"]:
                if not history["contradictory_pairs"] \
                        and history["overload_ticks"]:
                    indices.add(index)
                    break
        result[leg["id"]] = indices
    return result


def _joint_history_conflicts(case, alignment, worlds):
    """Legs that can only cross under mutually exclusive histories."""
    positive = _positive_worlds(case, alignment, worlds)
    claimants = [leg_id for leg_id, indices in positive.items() if indices]
    if len(claimants) < 2 or not worlds:
        return []
    common = set.intersection(*(positive[leg_id] for leg_id in claimants))
    if common:
        return []
    return [leg_id for leg_id in claimants]


def _duplicate_strip_ids(case):
    by_id = {}
    for strip in case["strips"]:
        by_id.setdefault(strip["id"], set()).add(strip["leg"])
    return {strip_id for strip_id, legs in by_id.items() if len(legs) > 1}


def evaluate_case(case, strip_history=None):
    """Evaluate one case document without any database write."""
    strip_history = strip_history or {}
    alignment = build_alignment(case)
    if case["runs"]:
        worlds, issues = build_worlds(case, alignment)
    else:
        # A strip-only package still needs an evaluation even though there is
        # no clock mesh to solve.
        worlds, issues = [], []
    duplicates = _duplicate_strip_ids(case)
    for strip_id, leg in strip_history.items():
        if leg not in {s["leg"] for s in case["strips"] if s["id"] == strip_id}:
            duplicates.add(strip_id)

    legs = []
    conflicts = [dict(issue) for issue in issues]
    document_conflict = any(x["kind"] in SEVERE_ISSUES for x in issues)
    cross_leg = _joint_history_conflicts(case, alignment, worlds) \
        if worlds else []
    for leg in case["legs"]:
        paired = evaluate_paired(case, alignment, worlds, leg)
        if leg["id"] in cross_leg:
            # Each leg could cross under some history, but no single history
            # makes every claimant cross.  The peaks must not be cross-cited.
            paired["status"] = "conflict"
            paired["tick_known"] = False
            paired["ticks"] = []
            paired["overload_ticks"] = []
            paired["missing_observations"] = _dedupe(
                paired["missing_observations"] + [
                    "one common history across the legs; a peak that crosses "
                    "this leg under one history and the next leg under another "
                    "history cannot sign both"])
        strip = evaluate_strips(case, leg)
        if strip["usable"] and any(x in duplicates for x in strip["usable"]):
            removed = [x for x in strip["usable"] if x in duplicates]
            strip["unusable"] = strip["unusable"] + [
                {"id": x, "reason": "same strip would sign another leg"}
                for x in removed]
            strip["usable"] = [x for x in strip["usable"] if x not in duplicates]
            strip["missing_observations"] = _dedupe(
                strip["missing_observations"] + [
                    "a distinct witness strip; one strip cannot sign two legs"])
            if not strip["usable"]:
                strip["status"] = "unusable"
        receipts, receipt_conflicts, receipt_missing = evaluate_receipts(
            case, leg, paired)
        result, leg_conflicts = _combine_leg(
            case, leg, paired, strip, receipts, receipt_conflicts)
        result["missing_observations"] = _dedupe(
            result["missing_observations"] + receipt_missing)
        if any(x["kind"] == "offset_unknown" for x in issues):
            result["missing_observations"] = _dedupe(
                result["missing_observations"] + [
                    "a documented clock offset (or sync pulse) for each run; "
                    "a calibration recorded after a reboot cannot back-fill "
                    "an earlier run"])
            if result["status"] == "overload":
                result["status"] = "unverified"
                result["tick_known"] = False
                result["ticks"] = []
                result["basis"] = None
        if document_conflict and result["status"] not in ("conflict",):
            result["status"] = "conflict"
            result["tick_known"] = False
            result["ticks"] = []
            result["basis"] = None
            result["missing_observations"] = _dedupe(
                result["missing_observations"] + [
                    "a single common history; per-piece explanations that do "
                    "not share one history are kept only as unverified material",
                ])
        if paired["status"] == "conflict" and result["status"] == "conflict":
            conflict = _issue(
                "no_common_history",
                "each peak is locally explainable under some history, but no "
                "single history covers both channels; conflicting material is "
                "kept and the leg stays unverified",
                [case["document_id"], leg["id"]], leg["id"])
            leg_conflicts.append(conflict)
        conflicts.extend(leg_conflicts)
        legs.append(result)

    for conflict in issues:
        if conflict.get("leg") is None:
            continue
        for leg in legs:
            if leg["leg"] == conflict["leg"] and leg["status"] != "conflict":
                leg["status"] = "conflict"
                leg["tick_known"] = False
                leg["ticks"] = []
                leg["basis"] = None

    return {"document": case["document_id"], "case": case["id"],
            "legs": legs, "conflicts": _dedupe_conflicts(conflicts)}


def _dedupe_conflicts(conflicts):
    seen = set()
    out = []
    for conflict in conflicts:
        key = (conflict.get("kind"), conflict.get("leg"),
               conflict.get("detail"))
        if key in seen:
            continue
        seen.add(key)
        out.append(conflict)
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _ensure(db):
    db.execute("""CREATE TABLE IF NOT EXISTS impact_witness_document(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id TEXT UNIQUE NOT NULL,
        case_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS impact_witness_packet(
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        leg TEXT NOT NULL,
        status TEXT NOT NULL,
        tick_known INTEGER NOT NULL,
        ticks TEXT NOT NULL,
        basis TEXT,
        evidence TEXT NOT NULL,
        seq INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS impact_witness_conflict(
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        leg TEXT,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL,
        evidence TEXT NOT NULL,
        seq INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS impact_witness_strip_use(
        strip_id TEXT PRIMARY KEY,
        leg TEXT NOT NULL,
        document_id TEXT NOT NULL,
        usable INTEGER NOT NULL)""")


def _next_seq(db, table):
    row = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM " + table).fetchone()
    return row[0]


def _existing_packets(db, leg):
    return [dict(row) for row in db.execute(
        "SELECT * FROM impact_witness_packet WHERE leg=? ORDER BY seq", (leg,))]


def _packet_relation(existing, result):
    """Classify how an existing packet relates to a new conclusion.

    A weaker phase-only conclusion does not contradict a tick conclusion, and
    a missing-observation packet says nothing about the leg's phase.
    """
    relations = []
    for packet in existing:
        old = packet["status"]
        new = result["status"]
        if old not in CONCLUSIVE or new not in CONCLUSIVE:
            continue
        old_positive = old in POSITIVE
        new_positive = new in POSITIVE
        old_tick = old == "overload" and bool(packet["tick_known"])
        new_tick = new == "overload" and result["tick_known"]
        if old_positive != new_positive:
            relations.append(("opposite", packet))
        elif old_tick and new_tick:
            old_ticks = [x["tick"] for x in json.loads(packet["ticks"])]
            if old_ticks != result["overload_ticks"]:
                relations.append(("tick", packet))
    return relations


def import_data(db, data):
    """Import one case document, keeping every earlier disposition intact.

    A bundle with a ``cases`` list is also accepted so one local file can
    carry several independent originals; each case keeps its own document id.
    """
    from .source import record

    if isinstance(data, dict) and isinstance(data.get("cases"), list) \
            and not any(key in data for key in ("id", "legs", "runs")):
        return [import_data(db, case) for case in data["cases"]]

    case = normalize_case(data)
    _ensure(db)
    evidence_item = {key: data[key] for key in data}
    evidence_item["id"] = "impact-witness:" + case["document_id"]
    if not record(db, "impact-witness", evidence_item):
        # Same original imported twice: replay is a no-op, never a rewrite.
        strip_history = {row["strip_id"]: row["leg"] for row in db.execute(
            "SELECT strip_id,leg FROM impact_witness_strip_use")}
        evaluation = evaluate_case(case, strip_history)
        evaluation["packets"] = [row["id"] for row in db.execute(
            "SELECT id FROM impact_witness_packet WHERE document_id=? ORDER BY seq",
            (case["document_id"],))]
        evaluation["conflicts"] = []
        evaluation["preserved"] = []
        return evaluation

    strip_history = {row["strip_id"]: row["leg"] for row in db.execute(
        "SELECT strip_id,leg FROM impact_witness_strip_use")}
    evaluation = evaluate_case(case, strip_history)

    packets = []
    conflicts = []
    preserved = []
    with db:
        db.execute(
            """INSERT OR IGNORE INTO impact_witness_document
               (document_id,case_id,evidence_id) VALUES (?,?,?)""",
            (case["document_id"], case["id"], evidence_item["id"]))

        for leg in evaluation["legs"]:
            existing = _existing_packets(db, leg["leg"])
            relations = _packet_relation(existing, leg)
            if relations:
                for relation, packet in relations:
                    kind = ("late_evidence_conflict"
                            if relation in ("opposite", "conflict_evidence")
                            else "late_tick_conflict")
                    detail = (
                        "already produced %s packet %s for %s; later evidence "
                        "%s is kept, the earlier packet is preserved"
                        % (packet["status"], packet["id"], leg["leg"],
                           leg["status"]))
                    _insert_conflict(db, case["document_id"], leg["leg"], kind,
                                     detail, [packet["id"], case["document_id"]])
                    conflicts.append({"kind": kind, "leg": leg["leg"],
                                      "detail": detail})
                    preserved.append(packet["id"])
                leg["status"] = "conflict"
                leg["tick_known"] = False
                leg["ticks"] = []
                leg["overload_ticks"] = []
                leg["basis"] = None
                leg["missing_observations"] = _dedupe(
                    leg["missing_observations"] + [
                        "adjudication of the later conflicting original; the "
                        "earlier packet remains as evidence"])

            packet_id = "%s:%s" % (case["document_id"], leg["leg"])
            db.execute(
                """INSERT OR REPLACE INTO impact_witness_packet
                   (id,document_id,leg,status,tick_known,ticks,basis,evidence,seq)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (packet_id, case["document_id"], leg["leg"], leg["status"],
                 int(leg["tick_known"]),
                 json.dumps(leg["ticks"], ensure_ascii=False, sort_keys=True),
                 leg["basis"],
                 json.dumps(_dedupe([case["document_id"]] + [
                     s["id"] for s in case["strips"]
                     if s["leg"] == leg["leg"]]), ensure_ascii=False,
                     sort_keys=True),
                 _next_seq(db, "impact_witness_packet")))
            packets.append(packet_id)

        for conflict in evaluation["conflicts"]:
            leg_id = conflict.get("leg")
            _insert_conflict(db, case["document_id"], leg_id,
                             conflict["kind"], conflict["detail"],
                             conflict.get("evidence", []))
            conflicts.append({"kind": conflict["kind"], "leg": leg_id,
                              "detail": conflict["detail"]})

        for strip in case["strips"]:
            usable = 1 if any(
                strip["id"] in leg["strip"]["usable"]
                for leg in evaluation["legs"]) else 0
            db.execute(
                """INSERT INTO impact_witness_strip_use
                   (strip_id,leg,document_id,usable) VALUES (?,?,?,?)
                   ON CONFLICT(strip_id) DO UPDATE SET
                     usable=MAX(usable,excluded.usable)""",
                (strip["id"], strip["leg"], case["document_id"], usable))

    result = dict(evaluation)
    result["packets"] = packets
    result["conflicts"] = conflicts
    result["preserved"] = _dedupe(preserved)
    return result


def _insert_conflict(db, document_id, leg, kind, detail, evidence):
    key = "%s:%s:%s" % (document_id, leg, kind)
    db.execute(
        """INSERT OR IGNORE INTO impact_witness_conflict
           (id,document_id,leg,kind,detail,evidence,seq)
           VALUES (?,?,?,?,?,?,?)""",
        (key, document_id, leg, kind, detail,
         json.dumps(_dedupe(evidence), ensure_ascii=False, sort_keys=True),
         _next_seq(db, "impact_witness_conflict")))


def snapshot(db):
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "impact_witness_packet" not in tables:
        return {"documents": [], "packets": [], "conflicts": [], "strips": []}

    def rows(table, order):
        return [dict(row) for row in db.execute(
            "SELECT * FROM %s ORDER BY %s" % (table, order))]

    return {
        "documents": rows("impact_witness_document", "seq")
        if "impact_witness_document" in tables else [],
        "packets": rows("impact_witness_packet", "seq"),
        "conflicts": rows("impact_witness_conflict", "seq")
        if "impact_witness_conflict" in tables else [],
        "strips": rows("impact_witness_strip_use", "strip_id")
        if "impact_witness_strip_use" in tables else [],
    }


def _dedupe(items):
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
