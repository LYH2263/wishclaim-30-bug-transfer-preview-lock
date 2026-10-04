"""认领转让托管：锁归属、escrow 拦截、TTL 口径与并发一致性测试。

覆盖用户现场报的每一条：
- 预览/托管期间墙卡认领人仍是原认领人（offer 不写 wishes.claimer）
- 未确认单存在时任何人再认领都失败
- 确认失败 / 非法目标人：锁归原认领人独占，不成空名、不双边占行
- 撤销、TTL 释放后可再领
- 确认成功：墙/详情/我的认领一起换到新人，expires_at 满额重开
- 投影倒计时在托管期按真实剩余秒，不伪造 86400
- 第三人认领与确认并发交错，绝不留下两人同时 claimed
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.db import connect
from app.modules.claim_transfer import locking
from app.modules.claim_transfer.projection import project_card

T0 = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
TTL = 100


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    c = connect()
    c.executescript(
        "CREATE TABLE wishes("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, note TEXT, status TEXT,"
        "claimer TEXT, claimed_at TEXT, expires_at TEXT, data_quality TEXT);"
        "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);"
    )
    c.executescript(locking.SCHEMA)
    c.execute("INSERT INTO settings(key,value) VALUES ('ttl_seconds',?)", (str(TTL),))
    c.commit()
    c.close()
    return connect


def _insert_claimed_wish(c, claimer="alice", remain=TTL, now=T0):
    exp = now + timedelta(seconds=remain)
    cur = c.execute(
        "INSERT INTO wishes(title,note,status,claimer,claimed_at,expires_at,data_quality)"
        " VALUES ('w','n','claimed',?,?,?,'clean')",
        (claimer, (now - timedelta(seconds=TTL)).isoformat(), exp.isoformat()),
    )
    c.commit()
    return cur.lastrowid


def _row(c, wid):
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    return dict(r)


# ---------------------------------------------------------------- offer/预览

def test_preview_keeps_original_claimer_everywhere(db):
    c = db()
    wid = _insert_claimed_wish(c, remain=40)
    res = locking.offer_transfer(c, wid, "alice", "bob", T0, TTL)
    assert res["ok"] and res["from_claimer"] == "alice" and res["target_claimer"] == "bob"

    w = _row(c, wid)
    # 预览返回后 wishes.claimer 必须仍是原认领人
    assert w["claimer"] == "alice"

    p = locking.pending_for_wish(c, wid)
    assert p and p["from_claimer"] == "alice" and p["target_claimer"] == "bob"

    # 墙/详情投影：认领人不变，倒计时按转让前真实剩余秒（40），不是满额
    card = project_card(w, T0 + timedelta(seconds=1), p)
    assert card["claimer"] == "alice"
    assert card["remain_seconds"] == 39
    assert card["pending_transfer"] is not None
    c.close()


def test_offer_rejects_invalid_target_without_touching_lock(db):
    c = db()
    wid = _insert_claimed_wish(c)
    before = _row(c, wid)
    for bad in ("", "   ", "alice", None):
        res = locking.offer_transfer(c, wid, "alice", bad, T0, TTL)
        assert res == {"ok": False, "reason": "invalid_target"}
        assert locking.pending_for_wish(c, wid) is None
    after = _row(c, wid)
    # 非法目标人绝不把锁写死给空名
    assert after["claimer"] == "alice"
    assert after["expires_at"] == before["expires_at"]
    c.close()


def test_offer_rejects_non_owner_and_double_offer(db):
    c = db()
    wid = _insert_claimed_wish(c)
    assert locking.offer_transfer(c, wid, "carol", "bob", T0, TTL)["reason"] == "not_owner"
    assert locking.offer_transfer(c, wid, "alice", "bob", T0, TTL)["ok"]
    assert locking.offer_transfer(c, wid, "alice", "dave", T0, TTL)["reason"] == "pending_transfer"
    assert _row(c, wid)["claimer"] == "alice"
    c.close()


# ----------------------------------------------------------------- escrow 拦截

def test_third_party_claim_blocked_while_pending(db):
    c = db()
    wid = _insert_claimed_wish(c)
    locking.offer_transfer(c, wid, "alice", "bob", T0, TTL)

    # 第三人、甚至目标人本人走直接 claim 都必须失败（目标人只能 confirm）
    for name in ("carol", "bob", "alice"):
        res = locking.claim_wish(db(), wid, name, T0 + timedelta(seconds=2), TTL)
        assert res["reason"] == "pending_transfer"
    assert _row(c, wid)["claimer"] == "alice"
    c.close()


def test_failed_confirm_leaves_lock_exclusively_with_original(db):
    c = db()
    wid = _insert_claimed_wish(c, remain=30)
    offer = locking.offer_transfer(c, wid, "alice", "bob", T0, TTL)
    tid = offer["id"]

    # 第三人在未确认阶段尝试认领（被拒）
    assert locking.claim_wish(db(), wid, "carol", T0, TTL)["reason"] == "pending_transfer"
    # 错误的人确认（不是目标人）
    assert locking.confirm_transfer(c, tid, "carol", T0 + timedelta(seconds=5), TTL)["reason"] == "not_target"

    w = _row(c, wid)
    assert w["claimer"] == "alice"                      # 锁归原认领人独占
    assert locking.pending_for_wish(c, wid) is not None  # 单子仍在，可继续确认/撤销
    # 原主与第三人的「我的认领」口径：只有 claimer=alice 命中，bob/carol 都不占行
    assert c.execute("SELECT COUNT(*) n FROM wishes WHERE claimer=?", ("alice",)).fetchone()["n"] == 1
    assert c.execute("SELECT COUNT(*) n FROM wishes WHERE claimer=?", ("bob",)).fetchone()["n"] == 0
    assert c.execute("SELECT COUNT(*) n FROM wishes WHERE claimer=?", ("carol",)).fetchone()["n"] == 0
    c.close()


def test_cancel_returns_lock_to_original_then_still_exclusive(db):
    c = db()
    wid = _insert_claimed_wish(c)
    tid = locking.offer_transfer(c, wid, "alice", "bob", T0, TTL)["id"]
    assert locking.cancel_transfer(c, tid, "alice")["ok"]
    assert locking.pending_for_wish(c, wid) is None
    w = _row(c, wid)
    assert w["claimer"] == "alice" and w["status"] == "claimed"
    # 撤销后别人仍不能抢（锁未过期，归原主独占）；TTL 到期才放回开放
    assert locking.claim_wish(db(), wid, "carol", T0, TTL)["reason"] == "locked"
    assert locking.claim_wish(db(), wid, "carol", T0 + timedelta(seconds=TTL + 1), TTL)["ok"]
    assert _row(c, wid)["claimer"] == "carol"
    c.close()


# ----------------------------------------------------------------- confirm 成功

def test_confirm_swaps_claimer_and_resets_full_ttl(db):
    c = db()
    wid = _insert_claimed_wish(c, remain=10)  # 转让前只剩 10 秒
    tid = locking.offer_transfer(c, wid, "alice", "bob", T0, TTL)["id"]
    confirm_at = T0 + timedelta(seconds=5)
    res = locking.confirm_transfer(c, tid, "bob", confirm_at, TTL)
    assert res["ok"] and res["claimer"] == "bob"

    w = _row(c, wid)
    assert w["claimer"] == "bob" and w["status"] == "claimed"
    # TTL 重开满额：expires_at = 确认时刻 + TTL，而非续转让前剩余秒
    assert w["expires_at"] == (confirm_at + timedelta(seconds=TTL)).isoformat()
    assert locking.pending_for_wish(c, wid) is None  # pending 单已结

    # 墙/详情/我的认领同一倒计时口径：满额剩余
    card = project_card(w, confirm_at, None)
    assert card["remain_seconds"] == TTL
    assert c.execute("SELECT COUNT(*) n FROM wishes WHERE claimer=?", ("alice",)).fetchone()["n"] == 0
    assert c.execute("SELECT COUNT(*) n FROM wishes WHERE claimer=?", ("bob",)).fetchone()["n"] == 1
    c.close()


def test_confirm_fails_after_ttl_releases_lock(db):
    c = db()
    wid = _insert_claimed_wish(c, remain=10)
    tid = locking.offer_transfer(c, wid, "alice", "bob", T0, TTL)["id"]
    # TTL 已过：sweep 释放愿望并把 pending 单置 expired
    from app.engines.claim_lock import release_if_expired
    later = T0 + timedelta(seconds=50)
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    rel = release_if_expired(r["status"], r["expires_at"], later)
    assert rel
    c.execute("UPDATE wishes SET status=?,claimer=NULL,claimed_at=NULL,expires_at=NULL WHERE id=?",
              ("open", wid))
    locking.expire_pending_transfers(c, later)
    c.commit()
    # 单子已 expired，确认必须失败；此时锁已放回开放，别人可领
    assert locking.confirm_transfer(c, tid, "bob", later, TTL)["reason"] == "transfer_not_pending"
    assert _row(c, wid)["claimer"] is None
    assert locking.claim_wish(db(), wid, "carol", later, TTL)["ok"]
    c.close()


# ----------------------------------------------------------------- 并发交错

@pytest.mark.parametrize("round_", range(20))
def test_concurrent_third_party_claim_vs_confirm_never_double_claimed(db, round_):
    c = db()
    wid = _insert_claimed_wish(c, remain=60)
    tid = locking.offer_transfer(c, wid, "alice", "bob", T0, TTL)["id"]
    c.close()

    outcomes = {}

    def do_confirm():
        cc = db()
        outcomes["confirm"] = locking.confirm_transfer(cc, tid, "bob", T0 + timedelta(seconds=1), TTL)
        cc.close()

    def do_claim():
        cc = db()
        outcomes["claim"] = locking.claim_wish(cc, wid, "carol", T0 + timedelta(seconds=1), TTL)
        cc.close()

    t1 = threading.Thread(target=do_confirm)
    t2 = threading.Thread(target=do_claim)
    t1.start(); t2.start(); t1.join(); t2.join()

    w = _row(db(), wid)
    # 无论两者谁先拿到写锁：
    #  - confirm 成功 → carol 必被 pending_transfer/locked 拒，最终 bob
    #  - claim 先跑也必被 pending 单拒，confirm 随后成功 → bob
    assert w["claimer"] in ("alice", "bob")
    assert w["claimer"] != "carol"
    if outcomes["confirm"]["ok"]:
        assert w["claimer"] == "bob"
        assert not outcomes["claim"]["ok"]
    else:
        assert w["claimer"] == "alice"
        assert not outcomes["claim"]["ok"]


def test_offer_claim_confirm_failure_interleaved_no_double_claimed(db):
    """预览（pending 已建）+ 第三人认领 + 确认失败叠在一起：只能有一个认领人。"""
    c = db()
    wid = _insert_claimed_wish(c, remain=60)
    tid = locking.offer_transfer(c, wid, "alice", "bob", T0, TTL)["id"]
    assert not locking.claim_wish(db(), wid, "carol", T0, TTL)["ok"]
    assert locking.confirm_transfer(c, tid, "impostor", T0, TTL)["reason"] == "not_target"

    w = _row(c, wid)
    n = c.execute("SELECT COUNT(DISTINCT claimer) n FROM wishes WHERE id=? AND claimer IS NOT NULL",
                  (wid,)).fetchone()["n"]
    assert n == 1 and w["claimer"] == "alice"
    # 还能用正确目标人收场
    assert locking.confirm_transfer(c, tid, "bob", T0 + timedelta(seconds=2), TTL)["ok"]
    assert _row(c, wid)["claimer"] == "bob"
    c.close()


# ----------------------------------------------------------------- API 端到端

def test_api_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from fastapi import HTTPException
    from app import seed, main

    seed.init_db()
    wid = main.create_wish(main.WishIn(title="手套"))["id"]
    main.claim(wid, main.ClaimIn(claimer="alice"))

    # 预览后墙卡仍是 alice
    r = main.transfer_preview(wid, main.TransferIn(claimer="alice", target="bob"))
    assert r["from_claimer"] == "alice"
    card = main.get_wish(wid)
    assert card["claimer"] == "alice" and card["pending_transfer"]["target_claimer"] == "bob"

    # 第三人再认领 409
    with pytest.raises(HTTPException) as ei:
        main.claim(wid, main.ClaimIn(claimer="carol"))
    assert ei.value.status_code == 409

    # 错误目标人确认 403，锁不变
    tid = card["pending_transfer"]["id"]
    with pytest.raises(HTTPException) as ei:
        main.transfer_confirm(tid, main.TransferConfirmIn(claimer="carol"))
    assert ei.value.status_code == 403
    assert main.get_wish(wid)["claimer"] == "alice"

    # /mine 不双边占行
    assert [w["id"] for w in main.mine(claimer="bob")] == []
    assert [w["id"] for w in main.mine(claimer="alice")] == [wid]

    # bob 确认成功 → 一起换人，到期满额重开
    old_exp = card["expires_at"]
    main.transfer_confirm(tid, main.TransferConfirmIn(claimer="bob"))
    card2 = main.get_wish(wid)
    assert card2["claimer"] == "bob" and card2["pending_transfer"] is None
    assert card2["expires_at"] != old_exp
    assert card2["remain_seconds"] > 80000  # 接近满额 86400，而非转让前剩余
    assert [w["id"] for w in main.mine(claimer="alice")] == []
    assert [w["id"] for w in main.mine(claimer="bob")] == [wid]
