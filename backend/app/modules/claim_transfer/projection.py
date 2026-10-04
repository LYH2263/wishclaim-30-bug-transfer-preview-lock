"""Wall/detail projection: pin the same claimer, expiry and countdown
everywhere, and surface the in-flight transfer marker.

Pure function over the wish row + pending offer — no SQL here so it is
trivially testable.

Single source of truth: the countdown is ALWAYS ``expires_at - now`` from
the ``wishes`` row. During escrow the lock (and its remaining seconds) is
still the original claimer's, so the wall keeps ticking down the pre-transfer
remainder; on confirm, ``expires_at`` is rewritten to a full window and wall,
detail and "mine" all flip together. No surface may synthesize its own 86400.
"""
from app.engines.claim_lock import parse_ts
from app.modules.claim_transfer.preview import TTL_POLICY

#: 托管/确认口径说明；倒计时本身不伪造，始终读 expires_at。
COUNTDOWN_PENDING_NOTE = "转让中：确认后倒计时重开满额，当前仍按转让前剩余秒跳动"


def project_card(wish: dict, now, pending: dict | None = None) -> dict:
    """Wall card / detail / mine projection.

    Fields guaranteed for consumers:
    - claimer / status straight from wishes (original claimer while pending;
      only a successful confirm replaces it)
    - expires_at + remain_seconds for ONE countdown shared by every surface
    - pending_transfer: escrow marker
    - ttl_policy: the pinned rule, so every surface agrees
    """
    card = dict(wish)
    card["ttl_policy"] = TTL_POLICY
    card["pending_transfer"] = pending
    remain = None
    if wish.get("status") == "claimed" and wish.get("expires_at"):
        # 唯一数据源：转让中也按真实 expires_at 跳转让前剩余秒，
        # 确认后 expires_at 被重写成满额窗口，三处一起换新。
        remain = max(0, int((parse_ts(wish["expires_at"]) - now).total_seconds()))
    card["remain_seconds"] = remain
    card["transfer_note"] = COUNTDOWN_PENDING_NOTE if pending else None
    return card
