"""里程归仓工具 HTTP API + 静态页面。"""

from __future__ import annotations

import csv
import io
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db

DB_PATH = os.environ.get("MILEAGE_DB", os.path.join(os.path.dirname(__file__), "data.db"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="公路观测里程归仓工具")


def conn():
    c = db.connect(DB_PATH)
    return c


def row_to_dict(r):
    return dict(r) if r is not None else None


# ---------------------------------------------------------------- 版本与断链

@app.get("/api/routes")
def list_routes():
    c = conn()
    rows = c.execute("SELECT DISTINCT route FROM route_version ORDER BY route").fetchall()
    recs = c.execute("SELECT DISTINCT route FROM record ORDER BY route").fetchall()
    return sorted({r["route"] for r in rows} | {r["route"] for r in recs})


@app.get("/api/versions")
def list_versions(route: str):
    c = conn()
    rows = c.execute(
        "SELECT * FROM route_version WHERE route=? ORDER BY effective_date, version_no", (route,)
    ).fetchall()
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["segments"] = [dict(s) for s in c.execute(
            "SELECT * FROM segment WHERE version_id=? ORDER BY seq", (r["id"],)).fetchall()]
        for s in d["segments"]:
            s["chain_end"] = s["chain_start"] + abs(s["read_end"] - s["read_start"])
        out.append(d)
    return out


class VersionIn(BaseModel):
    route: str
    effective_date: str
    note: str = ""
    copy_from: int | None = None


@app.post("/api/versions")
def create_version(body: VersionIn):
    c = conn()
    try:
        with c:
            vid = db.create_version(c, body.route, body.effective_date, body.note, body.copy_from)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": vid}


class SegmentsIn(BaseModel):
    segments: list[dict]


@app.put("/api/versions/{version_id}/segments")
def put_segments(version_id: int, body: SegmentsIn):
    c = conn()
    try:
        with c:
            segs = db.save_segments(c, version_id, body.segments)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return [{"label": s.label, "read_start": s.read_start, "read_end": s.read_end,
             "chain_start": s.chain_start, "chain_end": s.chain_end} for s in segs]


@app.delete("/api/versions/{version_id}")
def delete_version(version_id: int):
    c = conn()
    v = c.execute("SELECT * FROM route_version WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise HTTPException(404, "版本不存在")
    if v["status"] != "draft":
        raise HTTPException(400, "已发布版本不可删除；历史订正请新建版本")
    with c:
        c.execute("DELETE FROM route_version WHERE id=?", (version_id,))
    return {"ok": True}


@app.get("/api/versions/{version_id}/preview")
def preview(version_id: int):
    c = conn()
    try:
        return db.preview_version(c, version_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/versions/{version_id}/publish")
def publish(version_id: int):
    c = conn()
    try:
        with c:
            return db.publish_version(c, version_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- 记录

class ImportIn(BaseModel):
    csv_text: str
    source: str = ""


def parse_csv(text: str) -> list[dict]:
    """解析“路线,方向,里程牌读数,发生日期[,区段标记]”，首行可为表头。"""
    rows = []
    for line in csv.reader(io.StringIO(text.strip())):
        if not line or all(not c.strip() for c in line):
            continue
        cells = [c.strip() for c in line]
        try:
            reading = float(cells[2])
        except (ValueError, IndexError):
            joined = "".join(cells).lower()
            if "读数" in joined or "reading" in joined:
                continue  # 表头
            raise ValueError(f"无法解析的行: {line}")
        if len(cells) < 4:
            raise ValueError(f"列数不足: {line}")
        rows.append({"route": cells[0], "direction": cells[1], "reading": reading,
                     "occurred_date": cells[3], "marker": cells[4] if len(cells) > 4 else ""})
    return rows


@app.post("/api/records/import")
def import_records(body: ImportIn):
    c = conn()
    try:
        rows = parse_csv(body.csv_text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not rows:
        raise HTTPException(400, "没有可导入的数据行")
    with c:
        results = db.import_records(c, rows, body.source)
    summary = {"total": len(results)}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return {"summary": summary, "results": results}


@app.get("/api/records")
def list_records(route: str | None = None, status: str | None = None):
    c = conn()
    sql = "SELECT * FROM record WHERE 1=1"
    args: list = []
    if route:
        sql += " AND route=?"; args.append(route)
    if status:
        sql += " AND status=?"; args.append(status)
    sql += " ORDER BY id"
    out = []
    for r in c.execute(sql, args).fetchall():
        d = row_to_dict(r)
        d["placements"] = [row_to_dict(p) for p in c.execute(
            """SELECT p.*, s.label AS segment_label FROM placement p
               LEFT JOIN segment s ON s.id=p.segment_id
               WHERE p.record_id=? ORDER BY p.chain_pos""", (r["id"],)).fetchall()]
        if r["version_id"]:
            v = c.execute("SELECT version_no, effective_date FROM route_version WHERE id=?",
                          (r["version_id"],)).fetchone()
            d["version_no"], d["effective_date"] = v["version_no"], v["effective_date"]
        out.append(d)
    return out


class ResolveIn(BaseModel):
    placement_id: int


@app.post("/api/records/{record_id}/resolve")
def resolve(record_id: int, body: ResolveIn):
    c = conn()
    try:
        with c:
            db.resolve_record(c, record_id, body.placement_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------------------------------------------------------------- 反查与事实

@app.get("/api/reverse")
def reverse_api(version_id: int, chain: float):
    c = conn()
    v = c.execute("SELECT * FROM route_version WHERE id=?", (version_id,)).fetchone()
    if not v:
        raise HTTPException(404, "版本不存在")
    hits = db.reverse(c, version_id, chain)
    return {"version_id": version_id, "chain": chain, "readings": hits,
            "exception": None if hits else "连续位置超出该版路线范围"}


@app.get("/api/facts")
def facts(route: str | None = None, include_retracted: bool = True):
    c = conn()
    sql = """SELECT f.*, r.route, r.reading, r.occurred_date FROM fact_km f
             JOIN record r ON r.id=f.record_id WHERE 1=1"""
    args: list = []
    if route:
        sql += " AND r.route=?"; args.append(route)
    if not include_retracted:
        sql += " AND f.status='active'"
    sql += " ORDER BY f.id"
    rows = [row_to_dict(r) for r in c.execute(sql, args).fetchall()]
    agg: dict = {}
    for r in rows:
        if r["status"] != "active":
            continue
        key = (r["route"], r["bin_km"])
        agg[key] = agg.get(key, 0) + 1
    return {"facts": rows,
            "active_bins": [{"route": k[0], "bin_km": k[1], "count": v}
                            for k, v in sorted(agg.items())]}


# ---------------------------------------------------------------- 静态页

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
