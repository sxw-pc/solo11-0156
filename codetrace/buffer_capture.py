"""Capture a vendor buffer-export artifact from simulated inputs."""

from .buffer_archive import reconsider
from .source import record


def import_data(db, data):
    with db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS buffer_dump(
                id TEXT PRIMARY KEY, window_id TEXT NOT NULL, outcome TEXT NOT NULL
            )"""
        )
        results = []
        for item in data["dumps"]:
            if not record(db, "buffer-dump", item):
                continue
            entries = item["entries"]
            slots = item["slots"]
            if type(slots) is not int or slots <= 0 or len(entries) > slots:
                raise ValueError("invalid buffer descriptor")
            outcome = "full" if len(entries) == slots else "short_dump"
            db.execute("INSERT INTO buffer_dump VALUES (?,?,?)", (item["id"], item["window_id"], outcome))
            state = reconsider(db, item["window_id"], outcome, item["id"])
            results.append({"id": item["id"], "capture": outcome, "archive": state})
        return results


def snapshot(db):
    table_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='buffer_archive'"
    ).fetchone()
    if not table_exists:
        return []
    return [dict(row) for row in db.execute("SELECT * FROM buffer_archive ORDER BY window_id")]
