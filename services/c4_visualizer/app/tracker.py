"""centroid 매칭 추적 + 위치 이력 (docs/05-detection.md §5)."""
from __future__ import annotations

import statistics
from collections import OrderedDict
from dataclasses import dataclass, field

from shared.schemas import InferObject, Votes

# 운동 조건: 진행 반대 방향으로는 지름의 BACK_SLACK 까지만, 세로로는 Y_SLACK 까지만 움직였다고 본다
BACK_SLACK = 0.25
Y_SLACK = 0.35
# 프레임 간 이동량 투표: 표를 묶는 폭(지름 배수), 후보로 볼 득표 비율, seq 1 당 최대 이동(지름 배수)
VOTE_BIN = 0.15
VOTE_ALIAS = 0.8
MAX_STEP = 2.0


@dataclass
class Track:
    track_id: int
    bbox: tuple[int, int, int, int]
    cx: float
    cy: float
    last_seq: int
    prev_cx: float | None = None  # 직전 관측 위치 (검사선 통과 판단용)
    misses: int = 0
    partial: bool = False
    circularity: float | None = None
    solidity: float | None = None
    votes: Votes = field(default_factory=Votes)
    counted: bool = False
    verdict: str = "pending"  # counted 이후 확정값

    @property
    def diameter(self) -> float:
        return max(self.bbox[2], self.bbox[3])


class Tracker:
    def __init__(self, max_misses: int, match_max_dist: float, history_frames: int):
        self.max_misses = max_misses
        self.match_max_dist = match_max_dist
        self.history_frames = history_frames
        self.tracks: dict[int, Track] = {}
        self.history: OrderedDict[int, dict[int, tuple[int, int, int, int]]] = OrderedDict()
        self.next_id = 1
        # 컨베이어 위 과자는 모두 같은 속도로 움직이므로 x 속도(seq 1 당 px)는 트랙별이 아닌 세션 하나로 추정한다
        self.vx: float | None = None

    def clear(self):
        self.tracks.clear()
        self.history.clear()
        self.vx = None

    def _estimate_vx(self, seq: int, objects: list[InferObject], sign: int) -> float | None:
        """모든 (트랙, 검출) 쌍의 seq 1 당 x 이동량에 투표해 이번 프레임의 이동량을 고른다.

        과자는 모두 같은 속도라 진짜 이동량에 표가 몰린다. 그래서 프레임 드롭이 이어져 한 번에 많이 움직여도
        추적이 끊기지 않는다. 과자 간격이 일정하면 한 간격만큼 어긋난 값도 비슷하게 표를 받으므로,
        상위 후보 중 지금까지의 속도(처음엔 0)에 가장 가까운 것을 고른다.
        """
        vs, ds = [], []
        for t in self.tracks.values():
            gap = seq - t.last_seq
            if t.partial or gap <= 0:
                continue
            for o in objects:
                if o.partial:
                    continue
                d = max(t.diameter, o.bbox[2], o.bbox[3])
                dx = o.centroid[0] - t.cx
                if abs(o.centroid[1] - t.cy) > Y_SLACK * d or dx * sign < -BACK_SLACK * d or abs(dx) > MAX_STEP * d * gap:
                    continue
                vs.append(dx / gap)
                ds.append(d)
        if not vs:
            return None
        tol = VOTE_BIN * statistics.median(ds)
        vs.sort()
        votes, lo, hi = [], 0, 0
        for v in vs:  # 각 값 ±tol 안에 든 표 수 (정렬 후 두 포인터)
            while vs[lo] < v - tol:
                lo += 1
            while hi < len(vs) and vs[hi] <= v + tol:
                hi += 1
            votes.append(hi - lo)
        top = max(votes)
        if top < max(2, 0.3 * min(len(self.tracks), len(objects))):
            return None
        ref = self.vx or 0.0
        best = min((v for v, c in zip(vs, votes) if c >= VOTE_ALIAS * top), key=lambda v: abs(v - ref))
        return statistics.median(v for v in vs if abs(v - best) <= tol)

    def update(self, seq: int, objects: list[InferObject], direction: str) -> list[tuple[Track, InferObject]]:
        """fast 결과 1프레임으로 트랙을 갱신하고, 이번 프레임에 관측된 (트랙, 객체) 쌍을 돌려준다."""
        sign = -1 if direction == "rtl" else 1
        est = self._estimate_vx(seq, objects, sign)
        vx = est if est is not None else (self.vx or 0.0)
        cands = []
        for t in self.tracks.values():
            px = t.cx + vx * (seq - t.last_seq)  # 프레임 드롭으로 seq 가 건너뛰면 그만큼 더 이동했다고 예측
            for j, o in enumerate(objects):
                ox, oy = o.centroid
                d = max(t.diameter, o.bbox[2], o.bbox[3])
                if (ox - t.cx) * sign < -BACK_SLACK * d or abs(oy - t.cy) > Y_SLACK * d:
                    continue
                dist = ((ox - px) ** 2 + (oy - t.cy) ** 2) ** 0.5
                if dist <= self.match_max_dist * d:
                    cands.append((dist, t.track_id, j))

        # greedy: 가까운 쌍부터 확정
        pairs, used_t, used_o, speeds = [], set(), set(), []
        for _, tid, j in sorted(cands):
            if tid in used_t or j in used_o:
                continue
            used_t.add(tid)
            used_o.add(j)
            t, o = self.tracks[tid], objects[j]
            if not (t.partial or o.partial) and seq > t.last_seq:  # 경계에 잘린 윤곽선은 중심이 덜 움직인다
                speeds.append((o.centroid[0] - t.cx) / (seq - t.last_seq))
            self._observe(t, o, seq)
            pairs.append((t, o))
        if speeds:
            v = statistics.median(speeds)
            self.vx = v if self.vx is None else 0.7 * self.vx + 0.3 * v

        for j, o in enumerate(objects):
            if j not in used_o:
                t = Track(self.next_id, tuple(o.bbox), o.centroid[0], o.centroid[1], seq)
                self.next_id += 1
                self.tracks[t.track_id] = t
                self._observe(t, o, seq, new=True)
                pairs.append((t, o))

        for tid in list(self.tracks):
            if tid not in used_t and self.tracks[tid].last_seq != seq:
                t = self.tracks[tid]
                t.misses += 1
                if t.misses > self.max_misses:
                    del self.tracks[tid]

        self.history[seq] = {t.track_id: t.bbox for t, _ in pairs}
        while len(self.history) > self.history_frames:
            self.history.popitem(last=False)
        return pairs

    @staticmethod
    def _observe(t: Track, o: InferObject, seq: int, new: bool = False):
        t.prev_cx = None if new else t.cx
        t.cx, t.cy = o.centroid
        t.bbox = tuple(o.bbox)
        t.last_seq = seq
        t.misses = 0
        t.partial = o.partial
        if not o.partial:
            t.circularity, t.solidity = o.circularity, o.solidity

    def match_history(self, seq: int, objects: list[InferObject], min_iou: float = 0.3) -> list[tuple[Track, InferObject]]:
        """늦게 온 precise 결과를 history[seq] 의 bbox 와 IoU 로 매칭한다. 이미 사라진 트랙은 건너뛴다."""
        boxes = self.history.get(seq)
        if not boxes:
            return []
        cands = sorted(((iou(b, o.bbox), tid, j) for tid, b in boxes.items() for j, o in enumerate(objects)),
                       reverse=True)
        pairs, used_t, used_o = [], set(), set()
        for v, tid, j in cands:
            if v < min_iou:
                break
            if tid in used_t or j in used_o or tid not in self.tracks:
                continue
            used_t.add(tid)
            used_o.add(j)
            pairs.append((self.tracks[tid], objects[j]))
        return pairs


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    iw = min(ax + aw, bx + bw) - max(ax, bx)
    ih = min(ay + ah, by + bh) - max(ay, by)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    return inter / (aw * ah + bw * bh - inter)
