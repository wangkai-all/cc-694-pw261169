"""种子数据：G108 三个版本 + 覆盖全部手算基准的巡检记录。

基准场景：
- V1（2026-01-01）：直线 0→20，用于版本交接对比。
- V2（2026-06-01）：短链缺号 (10,12) + 长链重号 [13,15]。
- V3（2026-09-01）：逆向读数段（20→16）。
记录覆盖：交接日两侧、缺号异常、重号歧义（含区段标记消歧）、逆向段。
"""

import os

import db

CSV = """路线,方向,里程牌读数,发生日期,区段标记
G108,上行,5,2026-03-10,
G108,上行,12.6,2026-05-31,
G108,上行,12.6,2026-06-01,
G108,下行,11,2026-06-02,
G108,上行,10,2026-06-10,
G108,下行,12,2026-06-10,
G108,上行,14,2026-06-15,
G108,下行,14,2026-06-16,长链重复段
G108,上行,14,2026-06-17,短链后段
G108,下行,16,2026-06-20,
G108,上行,18,2026-06-25,
G108,下行,21,2026-06-26,
G108,上行,18,2026-10-05,
G108,下行,16,2026-10-06,
G108,上行,17,2026-10-07,逆向段
"""

VERSIONS = [
    ("2026-01-01", "初始版：全线直线", [
        {"label": "全线", "read_start": 0, "read_end": 20},
    ]),
    ("2026-06-01", "K10 改建：短链 10→12；K13-K15 长链重号", [
        {"label": "K0-K10主线", "read_start": 0, "read_end": 10},
        {"label": "短链后段", "read_start": 12, "read_end": 15},
        {"label": "长链重复段", "read_start": 13, "read_end": 16},
        {"label": "K16-K20主线", "read_start": 16, "read_end": 20},
    ]),
    ("2026-09-01", "K16-K20 改线：逆向读数段", [
        {"label": "K0-K16", "read_start": 0, "read_end": 16},
        {"label": "逆向段", "read_start": 20, "read_end": 16},
    ]),
]


def seed(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    with conn:
        for date, note, segs in VERSIONS:
            vid = db.create_version(conn, "G108", date, note)
            db.save_segments(conn, vid, segs)
            db.publish_version(conn, vid)
        from app import parse_csv
        db.import_records(conn, parse_csv(CSV), source="种子数据")
    conn.close()
    print(f"已写入种子数据：{path}")


if __name__ == "__main__":
    seed(os.environ.get("MILEAGE_DB", os.path.join(os.path.dirname(__file__), "data.db")))
