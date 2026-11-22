"""里程归仓核心域逻辑：断链分段映射、正反向换算、边界包含规则。

约定（全线统一，不随区段变化）：
- 读数 read：里程牌读数，单位 km，可正向也可逆向（read_end < read_start 表示沿连续位置方向读数递减）。
- 连续位置 chain：该版路线上的累计里程，从 origin（默认 0）起严格递增，段间连续。
- 段内映射等距：chain = chain_start + |read - read_start|。
- 边界包含：每个区段拥有闭区间 [min(read_start,read_end), max(...)] 内的全部读数。
  同一读数经不同区段换算：
    - 得到同一连续位置（连续接缝）→ 去重，只入仓一次，不重复计；
    - 得到不同连续位置（长链重号）→ 保留多个候选，等待区段标记或人工消歧，不默认取第一个。
  读数不在任何区段（短链缺号、超出范围）→ 异常。
- 反查：连续位置 → 全部对应读数，按读数值去重（连续接缝不重复，短链断点会报两个读数）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

EPS = 1e-6  # 读数/链位置比较精度（km，即 0.001 m 级）


@dataclass
class Segment:
    """一个映射区段。chain_start 由 build_segments 依序累加得到。"""

    seq: int
    label: str
    read_start: float
    read_end: float
    chain_start: float = 0.0
    id: int | None = None

    @property
    def span(self) -> float:
        return abs(self.read_end - self.read_start)

    @property
    def chain_end(self) -> float:
        return self.chain_start + self.span

    @property
    def read_lo(self) -> float:
        return min(self.read_start, self.read_end)

    @property
    def read_hi(self) -> float:
        return max(self.read_start, self.read_end)

    @property
    def reversed(self) -> bool:
        """逆向读数段：沿连续位置方向读数递减。"""
        return self.read_end < self.read_start

    def owns_reading(self, reading: float) -> bool:
        return self.read_lo - EPS <= reading <= self.read_hi + EPS

    def owns_chain(self, chain: float) -> bool:
        return self.chain_start - EPS <= chain <= self.chain_end + EPS

    def reading_to_chain(self, reading: float) -> float:
        return self.chain_start + abs(reading - self.read_start)

    def chain_to_reading(self, chain: float) -> float:
        d = chain - self.chain_start
        return self.read_start - d if self.reversed else self.read_start + d


@dataclass
class Candidate:
    """一个换算候选：同一连续位置可能由多个相邻区段共同给出（接缝），labels 记录来源。"""

    chain: float
    seg_ids: list[int] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)


@dataclass
class LocateResult:
    status: str  # located | ambiguous | exception
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""


def build_segments(specs: list[dict], origin: float = 0.0) -> list[Segment]:
    """按顺序构建区段并累加连续位置。校验：段长不能为 0。"""
    segs: list[Segment] = []
    chain = origin
    for i, spec in enumerate(specs):
        rs, re_ = float(spec["read_start"]), float(spec["read_end"])
        if abs(re_ - rs) < EPS:
            raise ValueError(f"第 {i + 1} 段读数起讫相同，段长为 0")
        seg = Segment(
            seq=i,
            label=str(spec.get("label") or f"区段{i + 1}"),
            read_start=rs,
            read_end=re_,
            chain_start=chain,
        )
        segs.append(seg)
        chain = seg.chain_end
    return segs


def locate(segments: list[Segment], reading: float, marker: str | None = None) -> LocateResult:
    """正向换算：读数 → 连续位置候选。

    marker（区段标记）用于消除长链重号歧义；标记与候选区段不匹配时进入异常，
    绝不默认取第一个位置。
    """
    merged: dict[float, Candidate] = {}
    for seg in segments:
        if not seg.owns_reading(reading):
            continue
        chain = round(seg.reading_to_chain(reading), 6)
        if chain in merged:
            c = merged[chain]
            c.seg_ids.append(seg.id if seg.id is not None else seg.seq)
            c.labels.append(seg.label)
        else:
            merged[chain] = Candidate(
                chain=chain,
                seg_ids=[seg.id if seg.id is not None else seg.seq],
                labels=[seg.label],
            )
    candidates = sorted(merged.values(), key=lambda c: c.chain)

    if marker:
        picked = [c for c in candidates if marker in c.labels]
        if not picked:
            return LocateResult(
                status="exception",
                candidates=candidates,
                reason=f"区段标记「{marker}」与该读数的候选区段不匹配",
            )
        if len(picked) > 1:
            return LocateResult(status="ambiguous", candidates=picked,
                                reason="区段标记仍对应多个位置")
        return LocateResult(status="located", candidates=picked)

    if not candidates:
        return LocateResult(status="exception", reason="读数不在该版路线的任何区段内（缺号或超界）")
    if len(candidates) > 1:
        return LocateResult(status="ambiguous", candidates=candidates, reason="长链重号：读数对应多个位置")
    return LocateResult(status="located", candidates=candidates)


def reverse_lookup(segments: list[Segment], chain: float) -> list[dict]:
    """反向换算：连续位置 → 读数列表（按读数值去重，接缝不重复）。"""
    out: dict[float, dict] = {}
    for seg in segments:
        if not seg.owns_chain(chain):
            continue
        reading = round(seg.chain_to_reading(chain), 6)
        entry = out.setdefault(reading, {"reading": reading, "labels": []})
        entry["labels"].append(seg.label)
    return [out[k] for k in sorted(out)]


def km_bin(chain: float) -> int:
    """下游公里区间事实的分箱：左闭右开 [n, n+1)。"""
    import math

    return int(math.floor(round(chain, 6)))
