"""引擎基准：版本交接日期、导入换算、发布时撤回/重建公里事实、人工消歧。

手算要点：
- 版本按发生日期选择，生效当日即用新版（effective_date <= occurred_date）。
- 读数 12.6：V1（直线 0→20）下链位 12.6、入 K12；V2（短链 10→12）下链位 10.6、入 K10。
- 发布新版后旧事实撤回（retracted）按新位置重建（active），原始读数 12.6 不动。
"""

import pytest

import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    yield c
    c.close()


def make_v1(c):
    vid = db.create_version(c, "G108", "2026-01-01", "初始版")
    db.save_segments(c, vid, [{"label": "全线", "read_start": 0, "read_end": 20}])
    db.publish_version(c, vid)
    return vid


def make_v2_draft(c):
    vid = db.create_version(c, "G108", "2026-06-01", "改建：短链+长链")
    db.save_segments(c, vid, [
        {"label": "K0-K10主线", "read_start": 0, "read_end": 10},
        {"label": "短链后段", "read_start": 12, "read_end": 15},
        {"label": "长链重复段", "read_start": 13, "read_end": 16},
        {"label": "K16-K20主线", "read_start": 16, "read_end": 20},
    ])
    return vid


def active_facts(c, record_id):
    return c.execute(
        "SELECT * FROM fact_km WHERE record_id=? AND status='active'", (record_id,)).fetchall()


def all_facts(c, record_id):
    return c.execute("SELECT * FROM fact_km WHERE record_id=? ORDER BY id", (record_id,)).fetchall()


# ------------------------------------------------------------- 版本交接日期

def test_version_handover_date(conn):
    make_v1(conn)
    v2 = make_v2_draft(conn)
    db.publish_version(conn, v2)
    # 生效前一日 → V1，读数 12.6 → 链位 12.6
    r1 = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 12.6,
                                   "occurred_date": "2026-05-31"}])[0]
    assert r1["status"] == "located" and r1["candidates"][0]["chain"] == 12.6
    # 生效当日 → V2，读数 12.6 → 链位 10.6
    r2 = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 12.6,
                                   "occurred_date": "2026-06-01"}])[0]
    assert r2["status"] == "located" and r2["candidates"][0]["chain"] == pytest.approx(10.6)
    # 同一读数 11：交接日前可定位，交接日后落入缺号区 → 异常
    r3 = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 11,
                                   "occurred_date": "2026-06-01"}])[0]
    assert r3["status"] == "exception"


def test_no_version_before_first_effective_date(conn):
    make_v1(conn)
    r = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 5,
                                  "occurred_date": "2025-12-31"}])[0]
    assert r["status"] == "exception"
    assert "没有已发布" in r["reason"]


# ------------------------------------------------------------- 发布：撤回再重建

def test_publish_retracts_and_rebuilds_facts(conn):
    make_v1(conn)
    # V2 尚未发布时导入 6 月记录：按 V1 入仓，链位 12.6，事实 K12
    rec = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 12.6,
                                    "occurred_date": "2026-06-15"}])[0]
    rid = rec["record_id"]
    assert [f["bin_km"] for f in active_facts(conn, rid)] == [12]

    v2 = make_v2_draft(conn)
    changes = db.preview_version(conn, v2)
    assert len(changes) == 1
    ch = changes[0]
    assert ch["old_chain"] == 12.6 and ch["new_chain"] == pytest.approx(10.6)

    db.publish_version(conn, v2)
    # 原始读数保留
    rec_row = conn.execute("SELECT * FROM record WHERE id=?", (rid,)).fetchone()
    assert rec_row["reading"] == 12.6 and rec_row["status"] == "located"
    # 旧事实 K12 撤回、新事实 K10 重建
    facts = all_facts(conn, rid)
    assert [(f["bin_km"], f["status"]) for f in facts] == [(12, "retracted"), (10, "active")]
    assert facts[0]["retracted_at"] is not None
    assert active_facts(conn, rid)[0]["chain_pos"] == pytest.approx(10.6)


def test_historical_correction_affects_only_its_window(conn):
    """历史订正：新版生效日早于既有记录日期才影响它；生效日前的记录不动。"""
    make_v1(conn)
    old = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 12.6,
                                    "occurred_date": "2026-03-01"}])[0]
    new = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 12.6,
                                    "occurred_date": "2026-07-01"}])[0]
    v2 = make_v2_draft(conn)
    db.publish_version(conn, v2)
    # 3 月记录仍属 V1：链位 12.6，事实 K12 仍然 active
    assert [f["bin_km"] for f in active_facts(conn, old["record_id"])] == [12]
    # 7 月记录改属 V2：K12 撤回，K10 重建
    assert [(f["bin_km"], f["status"]) for f in all_facts(conn, new["record_id"])] == [
        (12, "retracted"), (10, "active")]


# ------------------------------------------------------------- 歧义与消歧

def test_ambiguous_record_has_no_fact_until_resolved(conn):
    make_v1(conn)
    db.publish_version(conn, make_v2_draft(conn))
    rec = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 14,
                                    "occurred_date": "2026-06-15"}])[0]
    rid = rec["record_id"]
    assert rec["status"] == "ambiguous"
    assert [c["chain"] for c in rec["candidates"]] == [12, 14]
    assert active_facts(conn, rid) == []  # 歧义未消，不产生公里事实

    placements = conn.execute(
        "SELECT * FROM placement WHERE record_id=? ORDER BY chain_pos", (rid,)).fetchall()
    db.resolve_record(conn, rid, placements[1]["id"])  # 选定链位 14
    assert [f["bin_km"] for f in active_facts(conn, rid)] == [14]

    # 改选链位 12：K14 撤回，K12 重建
    db.resolve_record(conn, rid, placements[0]["id"])
    assert [(f["bin_km"], f["status"]) for f in all_facts(conn, rid)] == [
        (14, "retracted"), (12, "active")]


def test_marker_import_locates_directly(conn):
    make_v1(conn)
    db.publish_version(conn, make_v2_draft(conn))
    rec = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 14,
                                    "occurred_date": "2026-06-15", "marker": "长链重复段"}])[0]
    assert rec["status"] == "located"
    assert rec["candidates"][0]["chain"] == 14


def test_resolution_stable_within_same_version(conn):
    """人工消歧后：版本未变重算仍沿用选定；新版生效后旧选定失效，重新待消歧。"""
    make_v1(conn)
    db.publish_version(conn, make_v2_draft(conn))
    rec = db.import_records(conn, [{"route": "G108", "direction": "上行", "reading": 14,
                                    "occurred_date": "2026-06-15"}])[0]
    rid = rec["record_id"]
    pl = conn.execute("SELECT * FROM placement WHERE record_id=? ORDER BY chain_pos", (rid,)).fetchall()
    db.resolve_record(conn, rid, pl[1]["id"])  # 选定链位 14

    # 发布一个 7 月才生效的新版：该记录仍属 V2，重算后选定保持
    v3 = db.create_version(conn, "G108", "2026-07-01", "无关订正", copy_from=None)
    db.save_segments(conn, v3, [{"label": "全线", "read_start": 0, "read_end": 20}])
    db.publish_version(conn, v3)
    row = conn.execute("SELECT * FROM record WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "located"
    assert [f["bin_km"] for f in active_facts(conn, rid)] == [14]

    # 发布 6 月 1 日生效的历史订正（覆盖该记录）：旧选定失效，重新待消歧
    v4 = db.create_version(conn, "G108", "2026-06-01", "订正长链", copy_from=None)
    db.save_segments(conn, v4, [
        {"label": "K0-K10主线", "read_start": 0, "read_end": 10},
        {"label": "短链后段", "read_start": 12, "read_end": 15},
        {"label": "长链重复段", "read_start": 13.5, "read_end": 16},
        {"label": "K16-K20主线", "read_start": 16, "read_end": 20},
    ])
    db.publish_version(conn, v4)
    row = conn.execute("SELECT * FROM record WHERE id=?", (rid,)).fetchone()
    assert row["status"] == "ambiguous"
    assert active_facts(conn, rid) == []


# ------------------------------------------------------------- 反查

def test_reverse_via_db(conn):
    make_v1(conn)
    v2 = make_v2_draft(conn)
    db.publish_version(conn, v2)
    assert [h["reading"] for h in db.reverse(conn, v2, 10)] == [10, 12]
    assert db.reverse(conn, v2, 99) == []
