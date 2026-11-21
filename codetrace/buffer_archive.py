"""Receipt for a simulated imaging-buffer export."""

def reconsider(db, window_id, capture_state, source_id):
    state = "archivable" if capture_state == "full" else "needs_capture"
    db.execute(
        """CREATE TABLE IF NOT EXISTS buffer_archive(
            window_id TEXT PRIMARY KEY, state TEXT NOT NULL, source_id TEXT NOT NULL
        )"""
    )
    db.execute(
        """INSERT INTO buffer_archive VALUES (?,?,?)
           ON CONFLICT(window_id) DO UPDATE SET state=excluded.state,source_id=excluded.source_id""",
        (window_id, state, source_id),
    )
    return state
