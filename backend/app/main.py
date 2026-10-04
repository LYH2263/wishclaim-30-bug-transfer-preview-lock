from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.claim_lock import claim_allowed, lock_payload, release_if_expired
from app.modules.claim_transfer import locking as transfer_lock
from app.modules.claim_transfer.preview import TTL_POLICY
from app.modules.claim_transfer.projection import project_card

app = FastAPI(title="Wishclaim", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

def now(): return datetime.now(timezone.utc)

def ttl():
    c = connect(); row = c.execute("SELECT value FROM settings WHERE key='ttl_seconds'").fetchone(); c.close()
    return int(row["value"] if row else 86400)

def sweep(c):
    for r in c.execute("SELECT * FROM wishes WHERE status='claimed'"):
        rel = release_if_expired(r["status"], r["expires_at"], now())
        if rel:
            c.execute("UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? WHERE id=?",
                      (rel["status"], None, None, None, r["id"]))
    # 愿望被 TTL 释放后，挂在它上面的未确认转让单一并过期，解除托管拦截。
    transfer_lock.expire_pending_transfers(c, now())

def project(c, r):
    return project_card(dict(r), now(), transfer_lock.pending_for_wish(c, r["id"]))

@app.get("/api/health")
def health(): return {"ok": True, "project": "wishclaim"}

@app.get("/api/wishes")
def list_wishes():
    c = connect(); sweep(c); c.commit()
    rows = [project(c, r) for r in c.execute("SELECT * FROM wishes ORDER BY id DESC")]; c.close(); return rows

@app.get("/api/wishes/{wid}")
def get_wish(wid: int):
    c = connect(); sweep(c); c.commit()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r: c.close(); raise HTTPException(404, "not found")
    card = project(c, r); c.close(); return card

class WishIn(BaseModel):
    title: str
    note: str = ""

@app.post("/api/wishes")
def create_wish(body: WishIn):
    c = connect()
    cur = c.execute("INSERT INTO wishes(title,note,status,data_quality) VALUES (?,?,?,?)",
                    (body.title, body.note, "open", "clean"))
    c.commit(); wid = cur.lastrowid; c.close(); return {"id": wid}

class ClaimIn(BaseModel):
    claimer: str

@app.post("/api/wishes/{wid}/claim")
def claim(wid: int, body: ClaimIn):
    c = connect(); sweep(c); c.commit()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r: c.close(); raise HTTPException(404, "not found")
    # 托管中：存在未确认转让单时，任何人都不能直接 claim，目标人须走确认。
    blocked = transfer_lock.claim_blocked_by_pending(c, wid, body.claimer)
    if blocked:
        c.close(); raise HTTPException(409, blocked["reason"])
    allowed = claim_allowed(r["status"], r["claimer"], now(), r["expires_at"])
    if not allowed["ok"]:
        c.close(); raise HTTPException(409, allowed["reason"])
    p = lock_payload(body.claimer, now(), ttl())
    c.execute("UPDATE wishes SET status=?, claimer=?, claimed_at=?, expires_at=? WHERE id=?",
              (p["status"], p["claimer"], p["claimed_at"], p["expires_at"], wid))
    c.commit(); c.close(); return p

@app.post("/api/wishes/{wid}/release")
def release(wid: int):
    c = connect()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r: c.close(); raise HTTPException(404, "not found")
    if r["status"] != "claimed":
        c.close(); raise HTTPException(400, "not_claimed")
    c.execute("UPDATE wishes SET status='released', claimer=NULL, claimed_at=NULL, expires_at=NULL WHERE id=?", (wid,))
    transfer_lock.expire_pending_transfers(c, now())
    c.commit(); c.close(); return {"ok": True, "status": "released"}

@app.post("/api/wishes/{wid}/fulfill")
def fulfill(wid: int):
    c = connect()
    r = c.execute("SELECT * FROM wishes WHERE id=?", (wid,)).fetchone()
    if not r: c.close(); raise HTTPException(404, "not found")
    if r["status"] != "claimed":
        c.close(); raise HTTPException(400, "need_claim")
    c.execute("UPDATE wishes SET status='fulfilled' WHERE id=?", (wid,))
    transfer_lock.expire_pending_transfers(c, now())
    c.commit(); c.close(); return {"ok": True, "status": "fulfilled"}

class TransferIn(BaseModel):
    claimer: str
    target: str

# 失败原因 -> HTTP 状态码
_TF_STATUS = {
    "not_found": 404, "not_claimed": 400, "invalid_target": 400,
    "not_owner": 403, "not_target": 403,
    "pending_transfer": 409, "transfer_not_pending": 409,
    "wish_not_claimed": 409,
}

def _fail(res: dict):
    raise HTTPException(_TF_STATUS.get(res["reason"], 400), res["reason"])

@app.post("/api/wishes/{wid}/transfer")
def transfer_preview(wid: int, body: TransferIn):
    """当前认领人发起转让：生成 pending 转让单并返回预览（不改 wishes）。"""
    c = connect(); sweep(c); c.commit()
    res = transfer_lock.offer_transfer(c, wid, body.claimer, body.target, now(), ttl())
    if not res["ok"]:
        c.close(); _fail(res)
    c.commit(); c.close(); return res

class TransferConfirmIn(BaseModel):
    claimer: str

@app.post("/api/transfers/{tid}/confirm")
def transfer_confirm(tid: int, body: TransferConfirmIn):
    """目标人确认：写锁内换 claimer，TTL 自转让时刻重开满额。"""
    c = connect()
    res = transfer_lock.confirm_transfer(c, tid, body.claimer, now(), ttl())
    if not res["ok"]:
        c.close(); _fail(res)
    c.close(); return res

@app.post("/api/transfers/{tid}/cancel")
def transfer_cancel(tid: int, body: TransferConfirmIn):
    """原认领人撤销未确认的转让单。"""
    c = connect()
    res = transfer_lock.cancel_transfer(c, tid, body.claimer)
    if not res["ok"]:
        c.close(); _fail(res)
    c.commit(); c.close(); return {"ok": True, **res}

@app.get("/api/mine")
def mine(claimer: str):
    c = connect(); sweep(c); c.commit()
    rows = [project(c, r) for r in c.execute(
        "SELECT * FROM wishes WHERE claimer=? OR id IN (SELECT wish_id FROM claim_transfers WHERE status='pending' AND (from_claimer=? OR target_claimer=?))",
        (claimer, claimer, claimer))]
    c.close(); return rows

@app.get("/api/done")
def done():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM wishes WHERE status='fulfilled'")]; c.close(); return rows

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

@app.get("/api/rules")
def rules():
    return {
        "mutex": "同一愿望同时只能被一人认领",
        "ttl": "认领超时未核销则自动释放",
        "fulfill": "核销后状态变为 fulfilled",
        "transfer": "认领中可由当前认领人发起转让给目标人；存在未确认转让单时他人无法认领",
        "transferTtl": "转让确认后 TTL 自转让时刻重开满额（不继承剩余秒数），墙倒计时、详情到期时间以此为准",
        "ttlPolicy": TTL_POLICY,
    }
