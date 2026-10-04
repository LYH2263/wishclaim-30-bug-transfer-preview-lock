"""End-to-end regression tests for the claim-transfer state machine.

Covers, against a real SQLite DB (temp DATA_DIR):
- preview never moves the claimer; wall/detail/mine all keep the original
- pending offer blocks every /claim (third party AND the target)
- invalid/empty target never writes an empty claimer nor a pending row
- failed confirm / cancel leave the lock exclusively with the original owner
- successful confirm swaps claimer everywhere AND resets a full TTL window
- countdown is one source of truth (real remaining secs pending; full after)
- concurrent claim/offer/confirm can never leave two people claimed
- confirm after TTL expiry fails and releases the dead lock
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app import main
from app.db import connect
from app.modules.claim_transfer.projection import project_card
from app.modules.claim_transfer import locking as tl

TTL = 3600
T0 = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self): self.t = T0
    def __call__(self): return self.t


@pytest.fixture
def api(db, monkeypatch):
    clk = Clock()
    monkeypatch.setattr(main, "now", clk)
    monkeypatch.setattr(main, "ttl", lambda: TTL)
    return clk


def _exc(fn, *a, **k):
    """Return the HTTPException raised by fn, or None if it succeeded."""
    try:
        fn(*a, **k)
        return None
    except HTTPException as e:
        return e


def _row(wid):
    c = connect()
    r = dict(c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone())
    p = tl.pending_for_wish(c, wid)
    c.close()
    return r, p


def _new_wish(title="礼物"):
    return main.create_wish(main.WishIn(title=title, note=""))["id"]


def _claim(wid, name):
    return main.claim(wid, main.ClaimIn(claimer=name))


def _offer(wid, frm, target):
    return main.transfer_preview(wid, main.TransferIn(claimer=frm, target=target))


def _confirm(tid, by):
    return main.transfer_confirm(tid, main.TransferConfirmIn(claimer=by))


# --------------------------------------------------------------------------
# 1. 预览不改人
# --------------------------------------------------------------------------
def test_preview_keeps_original_claimer_everywhere(api):
    wid = _new_wish()
    _claim(wid, "alice")
    res = _offer(wid, "alice", "bob")
    assert res["ok"] is True
    assert res["from_claimer"] == "alice"
    assert res["target_claimer"] == "bob"
    assert res["ttl_policy"] == "reset_full"

    row, p = _row(wid)
    assert row["claimer"] == "alice"          # wish lock untouched
    assert row["status"] == "claimed"
    assert p is not None and p["from_claimer"] == "alice" and p["target_claimer"] == "bob"

    wall = main.get_wish(wid)
    assert wall["claimer"] == "alice"         # wall card still shows owner
    assert wall["pending_transfer"] is not None

    mine_a = [w["id"] for w in main.mine(claimer="alice")]
    mine_b = [w["id"] for w in main.mine(claimer="bob")]
    assert wid in mine_a                       # owner keeps the row
    assert wid not in mine_b                   # target does NOT pre-occupy it


# --------------------------------------------------------------------------
# 2. 未确认单期间任何人不能再认领
# --------------------------------------------------------------------------
def test_pending_blocks_all_claims(api):
    wid = _new_wish()
    _claim(wid, "alice")
    _offer(wid, "alice", "bob")

    e3 = _exc(main.claim, wid, main.ClaimIn(claimer="carol"))
    assert e3 and e3.status_code == 409 and e3.detail == "pending_transfer"

    # the target themselves must also go through /confirm, not /claim
    eb = _exc(main.claim, wid, main.ClaimIn(claimer="bob"))
    assert eb and eb.status_code == 409 and eb.detail == "pending_transfer"

    row, _ = _row(wid)
    assert row["claimer"] == "alice"


# --------------------------------------------------------------------------
# 3. 非法目标人不得写死空名 / 不留单
# --------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "   ", "alice", "  alice  "])
def test_invalid_target_leaves_lock_intact(api, bad):
    wid = _new_wish()
    _claim(wid, "alice")
    e = _exc(main.transfer_preview, wid, main.TransferIn(claimer="alice", target=bad))
    assert e and e.status_code == 400 and e.detail == "invalid_target"

    row, p = _row(wid)
    assert row["claimer"] == "alice"          # never blanked, never moved
    assert p is None                            # no escrow row left behind


def test_empty_claimer_name_rejected(api):
    wid = _new_wish()
    e = _exc(main.claim, wid, main.ClaimIn(claimer="   "))
    assert e and e.status_code == 400 and e.detail == "empty_claimer"
    row, _ = _row(wid)
    assert row["status"] == "open" and row["claimer"] is None


# --------------------------------------------------------------------------
# 4. 确认失败 → 锁归原认领人独占，不双边占
# --------------------------------------------------------------------------
def test_failed_confirm_keeps_lock_exclusive_to_owner(api):
    wid = _new_wish()
    _claim(wid, "alice")
    tid = _offer(wid, "alice", "bob")["id"]

    e = _exc(_confirm, tid, "carol")           # wrong person
    assert e and e.status_code == 403 and e.detail == "not_target"
    e2 = _exc(_confirm, tid, "")               # empty name
    assert e2 and e2.status_code == 403 and e2.detail == "not_target"

    row, p = _row(wid)
    assert row["claimer"] == "alice"
    assert p is not None                        # offer still open for bob

    mine_a = [w["id"] for w in main.mine(claimer="alice")]
    mine_b = [w["id"] for w in main.mine(claimer="bob")]
    assert wid in mine_a and wid not in mine_b  # never double-held


def test_cancel_keeps_lock_with_owner(api):
    wid = _new_wish()
    _claim(wid, "alice")
    tid = _offer(wid, "alice", "bob")["id"]
    assert main.transfer_cancel(tid, main.TransferConfirmIn(claimer="alice"))["ok"]

    row, p = _row(wid)
    assert row["claimer"] == "alice" and p is None
    # still a valid exclusive lock: a stranger cannot claim until TTL
    e = _exc(main.claim, wid, main.ClaimIn(claimer="carol"))
    assert e and e.detail == "locked"


# --------------------------------------------------------------------------
# 5 & 6. 确认成功 → 三处一起换人 + 满额重开，倒计时同源
# --------------------------------------------------------------------------
def test_confirm_success_swaps_claimer_and_resets_full_ttl(api):
    wid = _new_wish()
    _claim(wid, "alice")
    tid = _offer(wid, "alice", "bob")["id"]

    # half the original window burns away while the offer is pending
    api.t = T0 + timedelta(seconds=TTL // 2)
    res = _confirm(tid, "bob")
    assert res["ok"] and res["claimer"] == "bob" and res["ttl_policy"] == "reset_full"

    row, p = _row(wid)
    assert row["claimer"] == "bob" and p is None
    exp = datetime.fromisoformat(row["expires_at"])
    # full window measured from the CONFIRM instant, not remaining seconds
    assert int((exp - api.t).total_seconds()) == TTL

    wall = main.get_wish(wid)
    assert wall["claimer"] == "bob" and wall["pending_transfer"] is None
    assert wall["remain_seconds"] == TTL

    mine_a = [w["id"] for w in main.mine(claimer="alice")]
    mine_b = [w["id"] for w in main.mine(claimer="bob")]
    assert wid not in mine_a and wid in mine_b   # moved wholesale, never both


def test_countdown_uses_real_expires_at_while_pending(api):
    wid = _new_wish()
    _claim(wid, "alice")
    _offer(wid, "alice", "bob")

    elapsed = 100
    api.t = T0 + timedelta(seconds=elapsed)
    wall = main.get_wish(wid)
    # pending must show the OLD lock's real remaining seconds — never a
    # fabricated full 86400/TTL window while the row still ticks down.
    assert wall["remain_seconds"] == TTL - elapsed
    assert wall["transfer_note"] == "转让中：确认后倒计时重开满额"

    # pure projection agrees with the API surface on the same wish row
    c = connect()
    r = dict(c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone())
    p = tl.pending_for_wish(c, wid)
    c.close()
    card = project_card(r, api.t, p)
    assert card["claimer"] == "alice"
    assert card["remain_seconds"] == TTL - elapsed


# --------------------------------------------------------------------------
# 7. 并发：未确认阶段第三人再认领必失败；绝不两人同时 claimed
# --------------------------------------------------------------------------
def test_concurrent_claims_while_pending_all_fail(api):
    wid = _new_wish()
    _claim(wid, "alice")
    _offer(wid, "alice", "bob")

    results, barrier = [], threading.Barrier(4)

    def try_claim(name):
        barrier.wait()
        results.append((name, _exc(main.claim, wid, main.ClaimIn(claimer=name))))

    threads = [threading.Thread(target=try_claim, args=(n,))
               for n in ("carol", "dave", "erin")]

    def bad_confirm():
        barrier.wait()
        results.append(("carol-confirm", _exc(_confirm, _row(wid)[1]["id"], "carol")))

    threads.append(threading.Thread(target=bad_confirm))
    for t in threads: t.start()
    for t in threads: t.join()

    assert len(results) == 4
    for name, e in results:
        assert e is not None, f"{name} unexpectedly succeeded"
    # three /claim attempts refused by escrow; the impostor confirm refused
    # because carol is not the target — in every case the lock never moves.
    by_name = dict(results)
    for n in ("carol", "dave", "erin"):
        assert by_name[n].status_code == 409 and by_name[n].detail == "pending_transfer"
    assert by_name["carol-confirm"].status_code == 403
    assert by_name["carol-confirm"].detail == "not_target"
    row, _ = _row(wid)
    assert row["claimer"] == "alice"


def test_concurrent_offer_and_claim_never_double_holds(api):
    """A third-party claim racing a valid owner's offer never holds the lock.

    alice's lock is unexpired, so carol's /claim can NEVER succeed — the
    only question is whether she is refused by escrow (pending_transfer) or
    by the still-held lock (locked), depending on write-lock ordering. The
    invariant: alice remains sole holder and the offer is open.
    """
    wid = _new_wish()
    _claim(wid, "alice")
    outcomes, barrier = {}, threading.Barrier(2)

    def do_offer():
        barrier.wait()
        outcomes["offer"] = _exc(main.transfer_preview, wid,
                                 main.TransferIn(claimer="alice", target="bob"))

    def do_claim():
        barrier.wait()
        outcomes["claim"] = _exc(main.claim, wid, main.ClaimIn(claimer="carol"))

    ts = [threading.Thread(target=do_offer), threading.Thread(target=do_claim)]
    for t in ts: t.start()
    for t in ts: t.join()

    assert outcomes["offer"] is None                # owner's offer always opens
    assert outcomes["claim"] is not None
    assert outcomes["claim"].detail in ("pending_transfer", "locked")
    row, p = _row(wid)
    assert row["claimer"] == "alice" and p is not None
    assert p["from_claimer"] == "alice" and p["target_claimer"] == "bob"


def test_concurrent_expired_reclaim_racing_offer(api):
    """At TTL expiry an offer can't land on a dead lock; the reclaimer wins it."""
    wid = _new_wish()
    _claim(wid, "alice")
    api.t = T0 + timedelta(seconds=TTL + 1)        # alice's lock is dead

    e_offer = _exc(main.transfer_preview, wid,
                   main.TransferIn(claimer="alice", target="bob"))
    assert e_offer and e_offer.detail == "ttl_expired"
    # expired lock is reclaimable by anyone
    _claim(wid, "carol")
    row, p = _row(wid)
    assert row["claimer"] == "carol" and p is None


# --------------------------------------------------------------------------
# 8. 确认时 TTL 已过 → 失败并释放死锁，不把锁给目标人
# --------------------------------------------------------------------------
def test_confirm_after_ttl_expiry_fails_and_releases(api):
    wid = _new_wish()
    _claim(wid, "alice")
    tid = _offer(wid, "alice", "bob")["id"]

    api.t = T0 + timedelta(seconds=TTL + 10)
    e = _exc(_confirm, tid, "bob")
    assert e and e.status_code == 409 and e.detail == "ttl_expired"

    row, p = _row(wid)
    assert row["status"] == "open" and row["claimer"] is None  # not bob, not alice
    assert p is None

    # open again — anyone can now claim
    _claim(wid, "carol")
    row, _ = _row(wid)
    assert row["claimer"] == "carol"
