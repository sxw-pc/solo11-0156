"""Capture idealized, precombined load readings from a synthetic test fixture."""

from .impact_case import reconsider
from .source import record


def import_data(db, data):
    with db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS impact_capture(
                id TEXT PRIMARY KEY,leg TEXT NOT NULL,state TEXT NOT NULL
            )"""
        )
        out = []
        for event in data["captures"]:
            samples = event["combined"]
            if not samples or any(type(x) is not int or x < 0 for x in samples):
                raise ValueError("invalid synthetic impact samples")
            if not record(db, "impact-capture", event):
                continue
            state = "triggered" if max(samples) >= event["limit"] else "normal"
            db.execute("INSERT INTO impact_capture VALUES (?,?,?)", (event["id"], event["leg"], state))
            status = reconsider(db, event["leg"], state, event["id"])
            out.append({"capture": state, "case": status})
        return out


def snapshot(db):
    table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='impact_case'").fetchone()
    return [] if table is None else [dict(row) for row in db.execute("SELECT * FROM impact_case ORDER BY leg")]
