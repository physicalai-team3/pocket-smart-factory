"""검사선 통과 판정과 투표 기반 최종 verdict, 세션별 카운트."""
from __future__ import annotations

from shared.schemas import CountedEvent, Counts, InferObject, TrackConfig

from .tracker import Track

SPLIT_MINORITY = 0.3  # fast 투표에서 소수 쪽이 이 비율 이상이면 "갈림"


def vote(t: Track, o: InferObject, mode: str):
    """defect=null(partial 등 판정 불가)은 투표하지 않는다."""
    if o.defect is None:
        return
    v = t.votes
    if mode == "precise":
        if o.defect:
            v.precise_ng += 1
        else:
            v.precise_ok += 1
    elif o.defect:
        v.fast_ng += 1
    else:
        v.fast_ok += 1


def current_verdict(t: Track) -> str:
    """precise 투표가 있으면 precise 다수결, 없으면 fast 다수결. 동률이면 ng, 투표가 없으면 pending."""
    if t.counted:
        return t.verdict
    v = t.votes
    ok, ng = (v.precise_ok, v.precise_ng) if v.precise_ok + v.precise_ng else (v.fast_ok, v.fast_ng)
    if ok + ng == 0:
        return "pending"
    return "ok" if ok > ng else "ng"


def check_line(t: Track, cfg: TrackConfig, w: int, seq: int, counts: Counts) -> CountedEvent | None:
    """직전 관측 → 이번 관측 사이에 centroid 가 검사선을 진행 방향으로 넘었으면 1회 확정 카운트."""
    if t.counted or t.prev_cx is None:
        return None
    line = cfg.line_x * w
    crossed = t.prev_cx > line >= t.cx if cfg.direction == "rtl" else t.prev_cx < line <= t.cx
    if not crossed:
        return None
    # 투표가 하나도 없으면(끝까지 partial) 동률 규칙에 따라 ng 로 센다
    verdict = current_verdict(t)
    t.verdict = "ng" if verdict == "pending" else verdict
    t.counted = True
    counts.total += 1
    if t.verdict == "ok":
        counts.ok += 1
    else:
        counts.ng += 1
    return CountedEvent(track_id=t.track_id, verdict=t.verdict, seq=seq)


def needs_precise(t: Track, cfg: TrackConfig) -> bool:
    """docs/02-containers.md C4 4번: 미카운트 · 이번 프레임 비partial · precise 투표 없음 · 애매함."""
    v = t.votes
    if t.counted or t.partial or v.precise_ok + v.precise_ng:
        return False
    if t.circularity is not None and abs(t.circularity - cfg.circ_th) < cfg.circ_band:
        return True
    if t.solidity is not None and abs(t.solidity - cfg.solid_th) < cfg.solid_band:
        return True
    n = v.fast_ok + v.fast_ng
    return n > 0 and min(v.fast_ok, v.fast_ng) / n >= SPLIT_MINORITY
