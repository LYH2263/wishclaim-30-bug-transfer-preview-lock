"""Wall/detail projection: pin the same claimer, expiry and countdown
everywhere, and surface the in-flight transfer marker.

Pure function over the wish row + pending offer — no SQL here so it is
trivially testable.

Countdown invariant: ``remain_seconds`` is ALWAYS derived from the wish's
real ``expires_at`` — the pre-transfer lock's remaining seconds while an
offer is pending, the fresh full window after confirm. The pending marker
only carries a NOTE explaining the timer will reset on confirm; it must
never fabricate a full-window countdown while the wish row itself still
ticks down the old lock (that is what made the wall and detail disagree).
"""
from app.engines.claim_lock import parse_ts
from app.modules.claim_transfer.preview import TTL_POLICY

#: Countdown note while an offer is open. The number shown beside it is the
#: REAL remaining seconds; on confirm the timer restarts to a full window.
COUNTDOWN_PENDING_NOTE = "转让中：确认后倒计时重开满额"


def project_card(wish: dict, now, pending: dict | None = None) -> dict:
    """Wall card / detail row projection.

    Fields guaranteed for consumers:
    - claimer / status straight from wishes — ALWAYS the actual lock holder:
      the original claimer while pending, the new claimer only after confirm
    - expires_at + remain_seconds for the countdown (None when not claimed);
      one source of truth shared by wall, detail and 「我的认领」
    - pending_transfer: escrow marker (None unless an offer is open)
    - ttl_policy: the pinned rule, so every surface agrees
    """
    card = dict(wish)
    card["ttl_policy"] = TTL_POLICY
    card["pending_transfer"] = pending
    remain = None
    if wish.get("status") == "claimed" and wish.get("expires_at"):
        # Real remaining seconds in every state — never a fabricated value.
        remain = max(0, int((parse_ts(wish["expires_at"]) - now).total_seconds()))
    card["remain_seconds"] = remain
    card["transfer_note"] = COUNTDOWN_PENDING_NOTE if pending else None
    return card
