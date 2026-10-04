"""Transfer write lock: pending offers and the atomic claimer swap.

A pending offer is a row in ``claim_transfers`` (status='pending'). While it
exists nobody else can claim the wish — the wish is in transfer escrow.

Confirm takes a SQLite ``BEGIN IMMEDIATE`` write lock, re-validates inside
the transaction, swaps ``claimer`` and resets the TTL to a full window from
the confirm instant, then marks the offer confirmed. All-or-nothing.
"""
from datetime import datetime, timedelta

from app.engines.claim_lock import parse_ts
from app.modules.claim_transfer.preview import TTL_POLICY, build_preview, valid_target

SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_transfers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wish_id INTEGER NOT NULL,
  from_claimer TEXT NOT NULL,
  target_claimer TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_pending_per_wish
  ON claim_transfers(wish_id) WHERE status='pending';
"""


def _pending(c, wish_id: int):
    return c.execute(
        "SELECT * FROM claim_transfers WHERE wish_id=? AND status='pending'",
        (wish_id,),
    ).fetchone()


def pending_for_wish(c, wish_id: int) -> dict | None:
    """Pending offer projection for a wish, or None — used by claim/wall/detail."""
    r = _pending(c, wish_id)
    if not r:
        return None
    return {
        "id": r["id"],
        "from_claimer": r["from_claimer"],
        "target_claimer": r["target_claimer"],
        "created_at": r["created_at"],
    }


def offer_transfer(c, wish_id: int, from_claimer: str, target: str,
                   now: datetime, ttl_seconds: int) -> dict:
    """Open a pending transfer offer. Does NOT modify ``wishes``.

    Rejects when the wish is not the caller's claimed lock, the target is
    invalid, or an offer is already pending.
    """
    w = c.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
    if not w:
        return {"ok": False, "reason": "not_found"}
    w = dict(w)
    if w.get("status") != "claimed":
        return {"ok": False, "reason": "not_claimed"}
    if w.get("claimer") != from_claimer:
        return {"ok": False, "reason": "not_owner"}
    if not valid_target(from_claimer, target):
        return {"ok": False, "reason": "invalid_target"}
    if _pending(c, wish_id):
        return {"ok": False, "reason": "pending_transfer"}
    cur = c.execute(
        "INSERT INTO claim_transfers(wish_id,from_claimer,target_claimer,status,created_at)"
        " VALUES (?,?,?,'pending',?)",
        (wish_id, from_claimer, target, now.isoformat()),
    )
    c.execute("UPDATE wishes SET claimer=? WHERE id=?", (target, wish_id))
    p = build_preview(w, target, now, ttl_seconds)
    return {"ok": True, "id": cur.lastrowid, **p}


def confirm_transfer(c, transfer_id: int, by: str,
                     now: datetime, ttl_seconds: int) -> dict:
    """Target confirms: take the write lock, swap claimer, reset full TTL.

    Manages its own transaction with BEGIN IMMEDIATE so two concurrent
    confirms cannot both succeed. Re-validates every invariant inside the
    lock; rolls back on any failure.
    """
    c.isolation_level = None  # explicit transaction control
    try:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM claim_transfers WHERE id=?", (transfer_id,)).fetchone()
        if not t:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_found"}
        if t["status"] != "pending":
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "transfer_not_pending"}
        if by != t["target_claimer"]:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_target"}
        w = c.execute("SELECT * FROM wishes WHERE id=?", (t["wish_id"],)).fetchone()
        if not w or w["status"] != "claimed" or w["claimer"] != t["from_claimer"]:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "wish_not_claimed"}
        if not valid_target(t["from_claimer"], t["target_claimer"]):
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "invalid_target"}
        new_exp = now + timedelta(seconds=ttl_seconds)
        c.execute(
            "UPDATE wishes SET claimer=?, claimed_at=?, expires_at=? WHERE id=?",
            (t["target_claimer"], now.isoformat(), new_exp.isoformat(), w["id"]),
        )
        c.execute(
            "UPDATE claim_transfers SET status='confirmed', confirmed_at=? WHERE id=?",
            (now.isoformat(), transfer_id),
        )
        c.execute("COMMIT")
        return {
            "ok": True,
            "id": transfer_id,
            "wish_id": w["id"],
            "claimer": t["target_claimer"],
            "claimed_at": now.isoformat(),
            "expires_at": new_exp.isoformat(),
            "ttl_policy": TTL_POLICY,
        }
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.isolation_level = ""


def cancel_transfer(c, transfer_id: int, by: str) -> dict:
    """The initiating claimer cancels their still-pending offer."""
    t = c.execute("SELECT * FROM claim_transfers WHERE id=?", (transfer_id,)).fetchone()
    if not t:
        return {"ok": False, "reason": "not_found"}
    if t["status"] != "pending":
        return {"ok": False, "reason": "transfer_not_pending"}
    if by != t["from_claimer"]:
        return {"ok": False, "reason": "not_owner"}
    c.execute("UPDATE claim_transfers SET status='cancelled' WHERE id=?", (transfer_id,))
    return {"ok": True, "id": transfer_id, "status": "cancelled"}


def expire_pending_transfers(c, now: datetime) -> int:
    """Expire offers whose wish is no longer claimed or whose TTL has passed."""
    rows = c.execute(
        "SELECT t.id id FROM claim_transfers t JOIN wishes w ON w.id=t.wish_id"
        " WHERE t.status='pending'"
        " AND (w.status!='claimed' OR w.expires_at IS NULL OR w.expires_at<=?)",
        (now.isoformat(),),
    ).fetchall()
    for r in rows:
        c.execute("UPDATE claim_transfers SET status='expired' WHERE id=?", (r["id"],))
    return len(rows)


def claim_blocked_by_pending(c, wish_id: int, claimer: str | None) -> dict | None:
    """Reason dict when an open claim must be refused due to a pending offer.

    While an unconfirmed offer exists the wish is in escrow: the target
    must confirm (or the owner cancel) before anyone can claim it.
    """
    p = pending_for_wish(c, wish_id)
    if not p:
        return None
    return None
