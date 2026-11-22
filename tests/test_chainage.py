"""手算基准：长链重号、短链缺号、逆向读数、边界去重、正反转换、异常记录。

基准路线（README 中有同样的手算表）：

G108 V2（2026-06-01 生效）
  S1 K0-K10主线   读数  0→10   链位  0→10
     —— 短链：读数 (10,12) 缺号 ——
  S2 短链后段     读数 12→15   链位 10→13
     —— 长链：读数 [13,15] 重号 ——
  S3 长链重复段   读数 13→16   链位 13→16
  S4 K16-K20主线  读数 16→20   链位 16→20

G108 V3（2026-09-01 生效，逆向读数基准）
  S1 K0-K16       读数  0→16   链位  0→16
  S2 逆向段       读数 20→16   链位 16→20   （沿链位方向读数递减）
"""

import pytest

from chainage import build_segments, km_bin, locate, reverse_lookup


@pytest.fixture
def v2():
    return build_segments([
        {"label": "K0-K10主线", "read_start": 0, "read_end": 10},
        {"label": "短链后段", "read_start": 12, "read_end": 15},
        {"label": "长链重复段", "read_start": 13, "read_end": 16},
        {"label": "K16-K20主线", "read_start": 16, "read_end": 20},
    ])


@pytest.fixture
def v3():
    return build_segments([
        {"label": "K0-K16", "read_start": 0, "read_end": 16},
        {"label": "逆向段", "read_start": 20, "read_end": 16},
    ])


def chains(result):
    return [c.chain for c in result.candidates]


# ------------------------------------------------------------- 链位累加

def test_chain_layout(v2):
    assert [(s.chain_start, s.chain_end) for s in v2] == [
        (0, 10), (10, 13), (13, 16), (16, 20)]


# ------------------------------------------------------------- 短链缺号

def test_short_chain_boundaries_exist(v2):
    """断点两侧读数 10、12 都存在，都落在链位 10（同一物理点）。"""
    assert locate(v2, 10).status == "located"
    assert chains(locate(v2, 10)) == [10]
    assert locate(v2, 12).status == "located"
    assert chains(locate(v2, 12)) == [10]


def test_short_chain_missing_reading_is_exception(v2):
    """缺号区间 (10,12) 内的读数不存在 → 异常，绝不默认取第一个位置。"""
    r = locate(v2, 11)
    assert r.status == "exception"
    assert r.candidates == []


def test_short_chain_reverse_gives_two_readings(v2):
    """反查断点链位 10 → 两个读数 10 与 12。"""
    hits = reverse_lookup(v2, 10)
    assert [h["reading"] for h in hits] == [10, 12]


# ------------------------------------------------------------- 长链重号

def test_long_chain_duplicate_reading_is_ambiguous(v2):
    """读数 14 在 S2（链位 12）与 S3（链位 14）各出现一次 → 保留两个候选。"""
    r = locate(v2, 14)
    assert r.status == "ambiguous"
    assert chains(r) == [12, 14]


def test_long_chain_overlap_edges(v2):
    """重号区边缘：读数 13 → 链位 11/13；读数 15 → 链位 13/15。"""
    assert chains(locate(v2, 13)) == [11, 13]
    assert chains(locate(v2, 15)) == [13, 15]


def test_marker_disambiguates(v2):
    """区段标记消歧：读数 14 + 「长链重复段」→ 链位 14；+「短链后段」→ 链位 12。"""
    assert chains(locate(v2, 14, marker="长链重复段")) == [14]
    assert chains(locate(v2, 14, marker="短链后段")) == [12]


def test_bad_marker_is_exception(v2):
    """标记与候选区段矛盾 → 异常，而不是忽略标记。"""
    r = locate(v2, 14, marker="不存在的区段")
    assert r.status == "exception"


def test_long_chain_reverse(v2):
    """反查链位 12 → 读数 14（S2）；链位 14 → 读数 14（S3）：同一读数分属两链位。"""
    assert [h["reading"] for h in reverse_lookup(v2, 12)] == [14]
    assert [h["reading"] for h in reverse_lookup(v2, 14)] == [14]


# ------------------------------------------------------------- 边界去重

def test_continuous_joint_not_double_binned(v2):
    """连续接缝读数 16：S3 末端与 S4 首端算得同一链位 16 → 去重，只入仓一次。"""
    r = locate(v2, 16)
    assert r.status == "located"
    assert chains(r) == [16]
    assert set(r.candidates[0].labels) == {"长链重复段", "K16-K20主线"}


def test_reverse_at_joint_single_reading(v2):
    """反查接缝链位 16 → 只报一个读数 16。"""
    assert [h["reading"] for h in reverse_lookup(v2, 16)] == [16]


# ------------------------------------------------------------- 逆向读数

def test_reverse_direction_segment(v3):
    """逆向段：读数 18 → 链位 18；读数 20 → 链位 16（段首）；读数 17 → 链位 19。"""
    assert chains(locate(v3, 18)) == [18]
    assert chains(locate(v3, 20)) == [16]
    assert chains(locate(v3, 17)) == [19]


def test_reverse_direction_shared_reading_ambiguous(v3):
    """读数 16 是正向段末端（链位 16）也是逆向段末端（链位 20）→ 两个候选。"""
    r = locate(v3, 16)
    assert r.status == "ambiguous"
    assert chains(r) == [16, 20]
    assert chains(locate(v3, 16, marker="逆向段")) == [20]


def test_reverse_direction_reverse_lookup(v3):
    """反查：链位 16 → 读数 16（正向段末）与 20（逆向段首）；链位 19 → 读数 17。"""
    assert [h["reading"] for h in reverse_lookup(v3, 16)] == [16, 20]
    assert [h["reading"] for h in reverse_lookup(v3, 19)] == [17]


# ------------------------------------------------------------- 异常与分箱

def test_out_of_range(v2):
    assert locate(v2, 21).status == "exception"
    assert locate(v2, -1).status == "exception"
    assert reverse_lookup(v2, 21) == []


def test_km_bin_left_closed():
    """公里区间左闭右开：10.0 入 K10，9.999 入 K9。"""
    assert km_bin(10.0) == 10
    assert km_bin(9.999) == 9
    assert km_bin(10.6) == 10
