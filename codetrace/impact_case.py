"""Keep the current simulated transport evidence disposition."""

def reconsider(db, leg, capture_state, event_id):
    status = "overload_packet" if capture_state == "triggered" else "ordinary_packet"
    db.execute(
        """CREATE TABLE IF NOT EXISTS impact_case(
            leg TEXT PRIMARY KEY,status TEXT NOT NULL,evidence_id TEXT NOT NULL
        )"""
    )
    db.execute(
        """INSERT INTO impact_case VALUES (?,?,?)
           ON CONFLICT(leg) DO UPDATE SET status=excluded.status,evidence_id=excluded.evidence_id""",
        (leg, status, event_id),
    )
    return status
