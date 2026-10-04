"""Claimed-wish transfer hosting.

Three separately testable modules:
- preview.py: pure transfer preview, never writes wishes
- locking.py: pending-offer write lock + claimer swap under BEGIN IMMEDIATE
- projection.py: wall/detail projection (claimer, countdown, pending offer)

TTL policy is pinned once, in preview.TTL_POLICY: on confirm the lock is
reset to the full TTL from the transfer-confirm instant ("reset_full").
"""
