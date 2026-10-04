import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "wishclaim.db"

def connect():
    c = sqlite3.connect(db_path(), timeout=5)
    c.row_factory = sqlite3.Row
    # 写锁竞争时等待最多 5s（配合 BEGIN IMMEDIATE 串行化认领/转让），而非立刻抛 SQLITE_BUSY。
    c.execute("PRAGMA busy_timeout=5000")
    return c
