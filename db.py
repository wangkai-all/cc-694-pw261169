"""持久层与归仓引擎：版本选择、记录换算、发布时撤回/重建下游公里事实。

数据约定：
- record 永远保存原始读数（reading）与原始区段标记（marker），换算结果放在 placement。
- placement：某记录在某版本下的候选位置；chosen=1 表示当前入仓位置。
- fact_km：下游公里区间事实。连续位置变化时旧事实置 retracted（撤回），按新位置重建。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from chainage import build_segments, km_bin, locate, reverse_lookup

SCHEMA = """
CREATE TABLE IF NOT EXISTS route_version (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    effective_date TEXT NOT NULL,          -- YYYY-MM-DD，生效日期（含当日）
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | published
    note TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    published_at TEXT
);
CREATE TABLE IF NOT EXISTS segment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES route_version(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    label TEXT NOT NULL,
    read_start REAL NOT NULL,
    read_end REAL NOT NULL,
    chain_start REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS record (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route TEXT NOT NULL,
    direction TEXT NOT NULL,
    reading REAL NOT NULL,          -- 原始里程牌读数，永不改写
    occurred_date TEXT NOT NULL,    -- 发生日期，决定选用哪个路线版
    marker TEXT,                    -- 原始区段标记（可空）
    source TEXT DEFAULT '',
    version_id INTEGER,             -- 当前换算所用版本
    status TEXT NOT NULL DEFAULT 'exception',  -- located | ambiguous | exception
    exception_reason TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS placement (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES record(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL,
    segment_id INTEGER,             -- 候选来源区段（接缝合并时取首段）
    chain_pos REAL NOT NULL,
    chosen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_km (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES record(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL,
    bin_km INTEGER NOT NULL,
    chain_pos REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | retracted
    created_at TEXT NOT NULL,
    retracted_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ---------------------------------------------------------------- 版本

def load_segments(conn: sqlite3.Connection, version_id: int):
    rows = conn.execute(
        "SELECT * FROM segment WHERE version_id=? ORDER BY seq", (version_id,)
    ).fetchall()
    specs = [dict(r) for r in rows]
    segs = build_segments(specs) if not rows else _rebuild(rows)
    return segs


def _rebuild(rows) -> list:
    """从库中行重建 Segment（保留数据库 id 与已存 chain_start，并校验连续性）。"""
    from chainage import Segment

    segs = []
    for i, r in enumerate(rows):
        seg = Segment(
            seq=r["seq"],
            label=r["label"],
            read_start=r["read_start"],
            read_end=r["read_end"],
            chain_start=r["chain_start"],
            id=r["id"],
        )
        segs.append(seg)
        if i > 0 and abs(segs[i - 1].chain_end - seg.chain_start) > 1e-6:
            raise ValueError(f"区段 {i} 与 {i + 1} 的连续位置不衔接")
    return segs


def select_version(conn: sqlite3.Connection, route: str, date: str,
                   extra_published_id: int | None = None) -> sqlite3.Row | None:
    """按发生日期选版：生效日期 <= 发生日期 的最新已发布版（含当日生效）。

    extra_published_id：预览时把某个草稿视作已发布参与选择。
    """
    rows = conn.execute(
        """SELECT * FROM route_version
           WHERE route=? AND effective_date<=?
             AND (status='published' OR id=?)
           ORDER BY effective_date DESC, version_no DESC""",
        (route, date, extra_published_id if extra_published_id is not None else -1),
    ).fetchall()
    return rows[0] if rows else None


def create_version(conn: sqlite3.Connection, route: str, effective_date: str,
                   note: str = "", copy_from: int | None = None) -> int:
    cur = conn.execute("SELECT COALESCE(MAX(version_no),0)+1 AS n FROM route_version WHERE route=?",
                       (route,))
    version_no = cur.fetchone()["n"]
    cur = conn.execute(
        "INSERT INTO route_version(route,version_no,effective_date,note,created_at) VALUES(?,?,?,?,?)",
        (route, version_no, effective_date, note, now_iso()),
    )
    vid = cur.lastrowid
    if copy_from is not None:
        segs = load_segments(conn, copy_from)
        save_segments(conn, vid, [
            {"label": s.label, "read_start": s.read_start, "read_end": s.read_end} for s in segs
        ])
    return vid


def save_segments(conn: sqlite3.Connection, version_id: int, specs: list[dict]) -> list:
    """整体替换草稿版本的区段；chain_start 由 build_segments 自动累加，保证连续。"""
    v = conn.execute("SELECT * FROM route_version WHERE id=?", (version_id,)).fetchone()
    if not v or v["status"] != "draft":
        raise ValueError("只有草稿版本的断链区段可以编辑")
    segs = build_segments(specs)
    conn.execute("DELETE FROM segment WHERE version_id=?", (version_id,))
    for s in segs:
        conn.execute(
            "INSERT INTO segment(version_id,seq,label,read_start,read_end,chain_start) VALUES(?,?,?,?,?,?)",
            (version_id, s.seq, s.label, s.read_start, s.read_end, s.chain_start),
        )
    return segs


# ---------------------------------------------------------------- 记录换算

def _retract_facts(conn: sqlite3.Connection, record_id: int) -> None:
    conn.execute(
        "UPDATE fact_km SET status='retracted', retracted_at=? WHERE record_id=? AND status='active'",
        (now_iso(), record_id),
    )


def _build_fact(conn: sqlite3.Connection, record_id: int, version_id: int, chain_pos: float) -> None:
    conn.execute(
        "INSERT INTO fact_km(record_id,version_id,bin_km,chain_pos,status,created_at) VALUES(?,?,?,?, 'active',?)",
        (record_id, version_id, km_bin(chain_pos), chain_pos, now_iso()),
    )


def convert_record(conn: sqlite3.Connection, record_id: int,
                   extra_published_id: int | None = None,
                   write: bool = True) -> dict:
    """（重新）换算一条记录：撤回旧事实 → 选版 → 正向换算 → 落候选 → 按新位置重建事实。

    人工消歧的稳定性：版本未变时，若记录曾人工选定候选，重算仍沿用该选定；
    版本变化后旧选定失效，重新进入待消歧。write=False 时只试算（预览用），不落库。
    """
    rec = conn.execute("SELECT * FROM record WHERE id=?", (record_id,)).fetchone()
    ver = select_version(conn, rec["route"], rec["occurred_date"], extra_published_id)
    result = {"record_id": record_id, "version_id": None, "status": "exception",
              "candidates": [], "reason": ""}
    if ver is None:
        result["reason"] = "发生日期之前没有已发布的路线版本"
    else:
        segs = load_segments(conn, ver["id"])
        r = locate(segs, rec["reading"], rec["marker"])
        result.update(version_id=ver["id"], status=r.status, reason=r.reason,
                      candidates=[{"chain": c.chain, "labels": c.labels,
                                   "seg_id": c.seg_ids[0]} for c in r.candidates])
        if result["status"] == "ambiguous" and ver["id"] == rec["version_id"]:
            prev = conn.execute(
                "SELECT segment_id FROM placement WHERE record_id=? AND chosen=1",
                (record_id,)).fetchone()
            if prev:
                kept = [c for c in result["candidates"] if c["seg_id"] == prev["segment_id"]]
                if kept:
                    result.update(status="located", candidates=kept, reason="")
    if not write:
        return result

    _retract_facts(conn, record_id)
    conn.execute("DELETE FROM placement WHERE record_id=?", (record_id,))
    if result["candidates"]:
        chosen = 1 if result["status"] == "located" else 0
        for c in result["candidates"]:
            conn.execute(
                "INSERT INTO placement(record_id,version_id,segment_id,chain_pos,chosen,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (record_id, result["version_id"], c["seg_id"], c["chain"], chosen, now_iso()),
            )
        if result["status"] == "located":
            _build_fact(conn, record_id, result["version_id"], result["candidates"][0]["chain"])
    conn.execute(
        "UPDATE record SET version_id=?, status=?, exception_reason=? WHERE id=?",
        (result["version_id"], result["status"], result["reason"], record_id),
    )
    return result


def import_records(conn: sqlite3.Connection, rows: list[dict], source: str = "") -> list[dict]:
    out = []
    for row in rows:
        cur = conn.execute(
            "INSERT INTO record(route,direction,reading,occurred_date,marker,source,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (row["route"], row["direction"], float(row["reading"]), row["occurred_date"],
             row.get("marker") or None, source, now_iso()),
        )
        out.append(convert_record(conn, cur.lastrowid))
    return out


def resolve_record(conn: sqlite3.Connection, record_id: int, placement_id: int) -> None:
    """人工消歧：选定一个候选位置入仓，撤回旧事实并按选定位置重建。"""
    rec = conn.execute("SELECT * FROM record WHERE id=?", (record_id,)).fetchone()
    pl = conn.execute("SELECT * FROM placement WHERE id=? AND record_id=?",
                      (placement_id, record_id)).fetchone()
    if not rec or not pl:
        raise ValueError("记录或候选不存在")
    _retract_facts(conn, record_id)
    conn.execute("UPDATE placement SET chosen=0 WHERE record_id=?", (record_id,))
    conn.execute("UPDATE placement SET chosen=1 WHERE id=?", (placement_id,))
    conn.execute("UPDATE record SET status='located', exception_reason='' WHERE id=?", (record_id,))
    _build_fact(conn, record_id, pl["version_id"], pl["chain_pos"])


# ---------------------------------------------------------------- 预览与发布

def preview_version(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    """预览草稿发布的影响：把草稿视作已发布，逐记录试算并与现状对比。"""
    draft = conn.execute("SELECT * FROM route_version WHERE id=?", (version_id,)).fetchone()
    if not draft or draft["status"] != "draft":
        raise ValueError("只能预览草稿版本")
    recs = conn.execute("SELECT * FROM record WHERE route=? ORDER BY id", (draft["route"],)).fetchall()
    changes = []
    for rec in recs:
        trial = convert_record(conn, rec["id"], extra_published_id=version_id, write=False)
        cur = conn.execute(
            "SELECT chain_pos FROM placement WHERE record_id=? AND chosen=1", (rec["id"],)
        ).fetchone()
        old_chain = cur["chain_pos"] if cur else None
        new_chain = trial["candidates"][0]["chain"] if trial["status"] == "located" else None
        if (trial["version_id"] != rec["version_id"] or trial["status"] != rec["status"]
                or old_chain != new_chain):
            changes.append({
                "record_id": rec["id"], "occurred_date": rec["occurred_date"],
                "reading": rec["reading"], "marker": rec["marker"],
                "old_version_id": rec["version_id"], "old_status": rec["status"],
                "old_chain": old_chain,
                "new_version_id": trial["version_id"], "new_status": trial["status"],
                "new_chain": new_chain, "reason": trial["reason"],
            })
    return changes


def publish_version(conn: sqlite3.Connection, version_id: int) -> dict:
    """发布草稿：生效后对受影响记录重算，旧公里事实撤回、按新位置重建。原始读数不动。"""
    draft = conn.execute("SELECT * FROM route_version WHERE id=?", (version_id,)).fetchone()
    if not draft or draft["status"] != "draft":
        raise ValueError("只能发布草稿版本")
    if not conn.execute("SELECT 1 FROM segment WHERE version_id=?", (version_id,)).fetchone():
        raise ValueError("草稿没有任何区段，不能发布")
    changes = preview_version(conn, version_id)
    # 同一生效日期的历史订正：旧发布版置为已取代，选版时版本号大者优先
    conn.execute(
        "UPDATE route_version SET status='superseded'"
        " WHERE route=? AND effective_date=? AND status='published' AND id!=?",
        (draft["route"], draft["effective_date"], version_id))
    conn.execute("UPDATE route_version SET status='published', published_at=? WHERE id=?",
                 (now_iso(), version_id))
    for ch in changes:
        convert_record(conn, ch["record_id"])
    return {"version_id": version_id, "affected": len(changes), "changes": changes}


# ---------------------------------------------------------------- 查询

def reverse(conn: sqlite3.Connection, version_id: int, chain: float) -> list[dict]:
    return reverse_lookup(load_segments(conn, version_id), chain)
