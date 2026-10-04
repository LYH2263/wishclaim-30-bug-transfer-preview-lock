"""Pure transfer preview — reads state, assembles a preview, never writes.

TTL policy pin: ``TTL_POLICY = "reset_full"`` — the receiving claimer gets
a fresh full-TTL window at confirm time. This one constant feeds the
wall countdown, the detail-page expires_at explanation and the rules text.
"""
from datetime import datetime, timedelta

#: 转让确认后 TTL「转让时刻重开满额」，非继承剩余秒数。
TTL_POLICY = "reset_full"

def valid_target(claimer: str | None, target: str | None) -> bool:
    """Target must be a non-empty name distinct from the current claimer."""
    return bool(claimer) and bool(target) and target.strip() != "" and target != claimer


def build_preview(wish: dict, target: str, now: datetime, ttl_seconds: int) -> dict:
    """Assemble the transfer preview without touching ``wishes``.

    Returns ``{"ok": False, "reason": ...}`` when the offer cannot be made
    (wish not claimed, target invalid). On success returns the preview
    including the CURRENT ``expires_at`` and the would-be post-confirm
    ``new_expires_at`` (full TTL from now).
    """
    if wish.get("status") != "claimed" or not wish.get("claimer"):
        return {"ok": False, "reason": "not_claimed"}
    if not valid_target(wish.get("claimer"), target):
        return {"ok": False, "reason": "invalid_target"}
    cur_exp = wish.get("expires_at")
    new_exp = now + timedelta(seconds=ttl_seconds)
    return {
        "ok": True,
        "wish_id": wish["id"],
        "from_claimer": wish["claimer"],
        "target_claimer": target,
        "current_expires_at": cur_exp,
        "ttl_policy": TTL_POLICY,
        "new_expires_at": new_exp.isoformat(),
        "new_ttl_seconds": ttl_seconds,
        "reason": "",
    }
