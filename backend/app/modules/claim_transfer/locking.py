"""Transfer write lock: pending offers and the atomic claimer swap.

A pending offer is a row in ``claim_transfers`` (status='pending'). While it
exists nobody else can claim the wish — the wish is in transfer escrow.

Lock ownership policy (pinned):

- Pending offer / confirm failure / cancel: the claim stays EXCLUSIVELY with
  the original claimer (``from_claimer``). The ``wishes.claimer`` column is
  never touched until a successful confirm; escrow only ADDS the pending row.
  The lock returns to the open pool only via normal TTL release (handled by
  ``main.sweep``).

- Confirm success: claimer swap + TTL 重开满额 (``TTL_POLICY="reset_full"``),
  atomically inside one ``BEGIN IMMEDIATE`` transaction.

All mutating paths (offer / confirm / cancel / claim) take ``BEGIN IMMEDIATE``
and re-validate inside the lock, so a third party's claim racing a confirm can
never leave two people claimed at once.
"""
from datetime import datetime, timedelta

from app.engines.claim_lock import claim_allowed, lock_payload
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
    """Open a pending transfer offer. Does NOT modify ``wishes`` at all.

    The claimer column keeps pointing at the original claimer for the whole
    pending lifetime; wall/detail/mine therefore still show the original
    claimer until the target confirms. Rejects when the wish is not the
    caller's claimed lock, the target is invalid, or an offer is pending.

    Self-contained ``BEGIN IMMEDIATE`` transaction: validating and inserting
    the pending row is atomic, so an offer cannot be opened against a claim
    that a concurrent transaction just took or released.
    """
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        w = c.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
        if not w:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_found"}
        if w["status"] != "claimed":
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_claimed"}
        if w["claimer"] != from_claimer:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_owner"}
        if not valid_target(from_claimer, target):
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "invalid_target"}
        if _pending(c, wish_id):
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "pending_transfer"}
        cur = c.execute(
            "INSERT INTO claim_transfers(wish_id,from_claimer,target_claimer,status,created_at)"
            " VALUES (?,?,?,'pending',?)",
            (wish_id, from_claimer, target, now.isoformat()),
        )
        # wishes is deliberately untouched: preview keeps showing the
        # original claimer; confirm is the only place that swaps it.
        preview = build_preview(dict(w), target, now, ttl_seconds)
        c.execute("COMMIT")
        return {"ok": True, "id": cur.lastrowid, **preview}
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.isolation_level = ""


def confirm_transfer(c, transfer_id: int, by: str,
                     now: datetime, ttl_seconds: int) -> dict:
    """Target confirms: take the write lock, swap claimer, reset full TTL.

    Re-validates every invariant inside ``BEGIN IMMEDIATE``; rolls back on
    any failure, leaving the claim exclusively with ``from_claimer`` (never
    a null/empty name) and the offer still pending so it can be retried or
    cancelled. A racing third-party claim is blocked because it takes the
    same write lock and re-checks the pending row.
    """
    c.isolation_level = None
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
            # 非法目标人：绝不把锁写死给空名，锁归原认领人，单子保持 pending 可撤销。
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
    """The initiating claimer cancels their still-pending offer.

    Only flips the offer row; ``wishes`` is untouched because it never moved.
    After cancel the original claimer holds the lock exactly as before.
    """
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM claim_transfers WHERE id=?", (transfer_id,)).fetchone()
        if not t:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_found"}
        if t["status"] != "pending":
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "transfer_not_pending"}
        if by != t["from_claimer"]:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_owner"}
        c.execute("UPDATE claim_transfers SET status='cancelled' WHERE id=?", (transfer_id,))
        c.execute("COMMIT")
        return {"ok": True, "id": transfer_id, "status": "cancelled"}
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.isolation_level = ""


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


def escrow_reason(p: dict | None, claimer: str | None) -> dict | None:
    """Reason a direct claim is refused while an offer is open.

    Escrow is unilateral: while an offer is pending NOBODY can open-claim —
    not a third party and not even the target (the target must confirm). The
    only ways out are target-confirm or owner-cancel / TTL expiry.
    """
    if not p:
        return None
    return {"ok": False, "reason": "pending_transfer"}


def claim_wish(c, wish_id: int, claimer_name: str, now: datetime,
               ttl_seconds: int) -> dict:
    """Open-claim a wish under ``BEGIN IMMEDIATE``.

    Re-validates TTL/state AND transfer escrow inside the write lock. Without
    this a third-party claim landing between preview and confirm could observe
    no pending row / an expired lock and succeed, after which confirm would
    leave two people claimed. The single write lock makes claim vs. confirm
    strictly serial.
    """
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
        if not r:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_found"}
        blocked = escrow_reason(pending_for_wish(c, wish_id), claimer_name)
        if blocked:
            c.execute("ROLLBACK")
            return blocked
        allowed = claim_allowed(r["status"], r["claimer"], now, r["expires_at"])
        if not allowed["ok"]:
            c.execute("ROLLBACK")
            return allowed
        p = lock_payload(claimer_name, now, ttl_seconds)
        c.execute(
            "UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? WHERE id=?",
            (p["status"], p["claimer"], p["claimed_at"], p["expires_at"], wish_id),
        )
        c.execute("COMMIT")
        return {"ok": True, **p}
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.isolation_level = ""
