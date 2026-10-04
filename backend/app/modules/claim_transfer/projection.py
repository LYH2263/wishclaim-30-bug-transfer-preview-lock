"""Wall/detail projection: pin the same claimer, expiry and countdown
everywhere, and surface the in-flight transfer marker.

Pure function over the wish row + pending offer — no SQL here so it is
trivially testable.
"""
from datetime import timedelta

from app.engines.claim_lock import parse_ts
from app.modules.claim_transfer.preview import TTL_POLICY

#: Countdown shows the TTL-reset note while a transfer offer is open,
#: because on confirm the timer restarts to a full window.
COUNTDOWN_PENDING_NOTE = "转让中：确认后倒计时重开满额"


def project_card(wish: dict, now, pending: dict | None = None) -> dict:
    """Wall card / detail row projection.

    Fields guaranteed for consumers:
    - claimer / status straight from wishes (post-confirm already the new person)
    - expires_at + remain_seconds for the countdown (None when not claimed)
    - pending_transfer: escrow marker
    - ttl_policy: the pinned rule, so every surface agrees
    """
    card = dict(wish)
    card["ttl_policy"] = TTL_POLICY
    card["pending_transfer"] = pending
    remain = None
    if wish.get("status") == "claimed" and wish.get("expires_at"):
        remain = max(0, int((parse_ts(wish["expires_at"]) - now).total_seconds()))
        if pending:
            remain = max(0, int(timedelta(seconds=86400).total_seconds()))
    card["remain_seconds"] = remain
    if pending:
        card["transfer_note"] = COUNTDOWN_PENDING_NOTE
    else:
        card["transfer_note"] = None
    return card
