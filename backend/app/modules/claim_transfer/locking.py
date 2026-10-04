"""Transfer write lock: pending offers and the atomic claimer swap.

State-machine invariant (the single source of truth for all surfaces):

- ``wishes.claimer`` is ALWAYS the person who currently holds the lock.
  Opening a pending transfer offer NEVER touches ``wishes`` — the original
  claimer keeps exclusive ownership until (and unless) the target confirms.
- A pending row in ``claim_transfers`` is escrow metadata only. While it
  exists, nobody (including the target via /claim) can claim the wish; the
  target must confirm, the owner cancel, or the wish TTL expire.
- On confirm the claimer swap + full-TTL reset happen in one
  ``BEGIN IMMEDIATE`` transaction. Any failure rolls back, so the lock stays
  exclusively with the original claimer — it is never opened up and never
  held by two people at once.
- On cancel/expire only the transfer row changes; ``wishes`` was never
  modified, so no repair/restore step is needed or possible to forget.
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
    """Open a pending transfer offer under a write lock. NEVER writes ``wishes``.

    Manages its own BEGIN IMMEDIATE transaction so the owner re-validation
    and the offer insert are atomic against a concurrent claim. Rejects when
    the wish is not the caller's claimed lock, the target is invalid
    (empty/blank/self), or an offer is already pending.
    """
    target = (target or "").strip()
    c.isolation_level = None  # explicit transaction control
    try:
        c.execute("BEGIN IMMEDIATE")
        w = c.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
        if not w:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_found"}
        if w["status"] != "claimed":
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_claimed"}
        if not from_claimer or w["claimer"] != from_claimer:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_owner"}
        # 发起方的锁已过 TTL（sweep 尚未跑到）：锁已失效，不能转让。
        if not w["expires_at"] or parse_ts(w["expires_at"]) <= now:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "ttl_expired"}
        if not valid_target(w["claimer"], target):
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "invalid_target"}
        if _pending(c, wish_id):
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "pending_transfer"}
        # Escrow row only — the wish lock stays with from_claimer.
        cur = c.execute(
            "INSERT INTO claim_transfers(wish_id,from_claimer,target_claimer,status,created_at)"
            " VALUES (?,?,?,'pending',?)",
            (wish_id, from_claimer, target, now.isoformat()),
        )
        p = build_preview(dict(w), target, now, ttl_seconds)
        c.execute("COMMIT")
        return {"ok": True, "id": cur.lastrowid, **p}
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.isolation_level = ""


def confirm_transfer(c, transfer_id: int, by: str,
                     now: datetime, ttl_seconds: int) -> dict:
    """Target confirms: take the write lock, swap claimer, reset full TTL.

    Re-validates every invariant inside BEGIN IMMEDIATE so two concurrent
    confirms, or a confirm racing a third-party claim, cannot both succeed.
    On ANY failure the transaction rolls back: wishes.claimer was never
    changed before the commit, so the lock stays exclusively with the
    original claimer (never opened, never double-held).
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
        if not by or by != t["target_claimer"]:
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "not_target"}
        w = c.execute("SELECT * FROM wishes WHERE id=?", (t["wish_id"],)).fetchone()
        if not w or w["status"] != "claimed" or w["claimer"] != t["from_claimer"]:
            # Claimer moved (third party) or the wish was released/fulfilled.
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "wish_not_claimed"}
        if not valid_target(t["from_claimer"], t["target_claimer"]):
            c.execute("ROLLBACK")
            return {"ok": False, "reason": "invalid_target"}
        # Pending TTL ran out before confirm: release the dead wish lock and
        # terminate the escrow in this same transaction, so the wish is
        # immediately claimable again rather than blocking everyone behind a
        # stale pending row.
        if not w["expires_at"] or parse_ts(w["expires_at"]) <= now:
            c.execute(
                "UPDATE wishes SET status='open', claimer=NULL, claimed_at=NULL, expires_at=NULL"
                " WHERE id=?",
                (w["id"],),
            )
            c.execute(
                "UPDATE claim_transfers SET status='expired' WHERE id=?",
                (transfer_id,),
            )
            c.execute("COMMIT")
            return {"ok": False, "reason": "ttl_expired"}
        # Success policy (pinned in preview.TTL_POLICY): reset_full — the
        # receiving claimer gets a fresh full-TTL window from THIS instant.
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

    Only the transfer row changes. The wish lock was never moved, so the
    original claimer simply continues to hold it.
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
        if not by or by != t["from_claimer"]:
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
    """Expire offers whose wish is no longer claimed or whose TTL has passed.

    Only the transfer row is touched — wishes.claimer already reflects the
    original owner (or the released/fulfilled state), never the target.
    """
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
    """Reason dict when a claim must be refused due to a pending offer.

    While an unconfirmed offer exists the wish is in escrow: the target may
    ONLY take it through /transfers/{id}/confirm, the owner through cancel,
    and every other /claim attempt (by anyone, including the two parties) is
    refused. Returns None when nothing blocks the claim.
    """
    if pending_for_wish(c, wish_id):
        return {"ok": False, "reason": "pending_transfer"}
    return None
