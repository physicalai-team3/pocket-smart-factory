"""합성 프레임 생성기 — web/sim/index.html 과 같은 파라미터로 프레임 + 정답을 만든다.

정답(annotations)은 시뮬레이터 '정답 라벨 JSON' 과 같은 키를 쓴다 (bbox, category, defect_type, roundness).
camera 모드는 캔버스를 원근 변환으로 더 큰 프레임에 붙여 폰 촬영을 흉내 내고, 캔버스 네 모서리의 정답 좌표를 돌려준다.

사용 예:
    scene = Scene(seed=0)
    for _ in range(30):
        scene.step(0.1)
    canvas, anns = scene.render()
    frame, corners = camera_view(canvas, seed=0)
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import cv2
import numpy as np

SEG = 180
SCAN = 0.34  # 시뮬레이터 검사선 (화면 폭 비율)
TYPES = ("bite", "cut", "wedge", "half")

BG = (27, 24, 20)  # #14181b (BGR)
BELT = (49, 46, 42)  # #2a2e31
CLEAT = (60, 58, 55)
RIM = (20, 54, 96)  # rgba(96,54,20)
CHIP = (17, 36, 64)  # rgba(64,36,17)


def make_shape(rng: random.Random, is_defect: bool, strength: float) -> tuple[str, list[float]]:
    """시뮬레이터 makeShape() 의 Python 이식. (유형, 각도별 반경 비율 SEG개)"""
    s = strength / 100
    typ = TYPES[rng.randrange(len(TYPES))] if is_defect else "round"
    dirn = rng.random() * 2 * math.pi
    k = s * (0.55 + rng.random() * 0.45)
    bite_r = 0.35 + k * 0.55
    bite_d = 1 + bite_r * (0.62 - k * 0.34)
    cut_d = 0.86 - k * 0.52
    half_d = 0.17 - k * 0.15
    wedge_w = 0.22 + k * 0.5
    rad = []
    for i in range(SEG):
        a = i / SEG * 2 * math.pi
        d = ((a - dirn + math.pi * 3) % (2 * math.pi)) - math.pi
        c = math.cos(d)
        r = 1.0
        if typ == "bite":
            b = bite_d * c
            disc = b * b - (bite_d ** 2 - bite_r ** 2)
            if disc > 0:
                t = b - math.sqrt(disc)
                if 0 < t < 1:
                    r = t
        elif typ in ("cut", "half"):
            dd = cut_d if typ == "cut" else half_d
            if c > 0.001:
                r = min(1.0, dd / c)
        elif typ == "wedge" and abs(d) < wedge_w:
            r = 0.07 + abs(d) / wedge_w * 0.1
        rad.append(max(0.06, r))
    return typ, rad


@dataclass
class Cookie:
    id: int
    line: int
    x: float
    r: float
    rot: float
    defect: bool
    typ: str
    rad: list[float]
    tone: float
    chips: list[tuple[float, float, float]]
    counted: bool = False

    def polygon(self, cy: float) -> np.ndarray:
        pts = [(self.x + math.cos(i / SEG * 2 * math.pi + self.rot) * self.r * self.rad[i],
                cy + math.sin(i / SEG * 2 * math.pi + self.rot) * self.r * self.rad[i]) for i in range(SEG)]
        return np.array(pts, np.float32)


@dataclass
class Scene:
    seed: int = 0
    lines: int = 3
    line_gap: int = 150
    speed: float = 130.0  # px/s
    size: int = 78
    gap: int = 90
    defect_rate: float = 0.30
    strength: float = 55.0
    width: int = 1316
    cookies: list[Cookie] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {"total": 0, "ng": 0})

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self.height = self.lines * self.line_gap
        self.next_id = 1
        self.belt_off = 0.0
        pitch = self.gap + self.size
        self.last_x = [self.width - self.rng.random() * pitch for _ in range(self.lines)]

    def line_y(self, i: int) -> float:
        return i * self.line_gap + self.line_gap / 2

    def _add(self, line: int, x: float):
        is_defect = self.rng.random() < self.defect_rate
        typ, rad = make_shape(self.rng, is_defect, self.strength)
        chips = []
        for _ in range(3 + self.rng.randrange(6)):
            a, rho = self.rng.random() * 2 * math.pi, math.sqrt(self.rng.random()) * 0.62
            if rho < rad[int(a / (2 * math.pi) * SEG) % SEG] * 0.82:
                chips.append((math.cos(a) * rho, math.sin(a) * rho, 0.055 + self.rng.random() * 0.055))
        self.cookies.append(Cookie(self.next_id, line, x, self.size / 2, self.rng.random() * 2 * math.pi,
                                   is_defect, typ, rad, 0.88 + self.rng.random() * 0.24, chips))
        self.next_id += 1

    def step(self, dt: float):
        dx = self.speed * dt
        self.belt_off = (self.belt_off + dx) % 44
        scan_x = self.width * SCAN
        for c in self.cookies:
            px, c.x = c.x, c.x - dx
            if not c.counted and px > scan_x >= c.x:
                c.counted = True
                self.stats["total"] += 1
                self.stats["ng"] += int(c.defect)
        self.cookies = [c for c in self.cookies if c.x + c.r > -4]
        pitch = self.gap + self.size
        for i in range(self.lines):
            self.last_x[i] -= dx
            if self.last_x[i] <= self.width - pitch:
                x = self.width + self.size / 2
                self._add(i, x)
                self.last_x[i] = x

    def render(self, draw_scan: bool = False) -> tuple[np.ndarray, list[dict]]:
        img = np.full((self.height, self.width, 3), BG, np.uint8)
        for i in range(self.lines):
            bh = max(26, self.line_gap - 16)
            top = int(self.line_y(i) - bh / 2)
            cv2.rectangle(img, (0, top), (self.width, top + bh), BELT, -1)
            x = -self.belt_off
            while x < self.width:
                cv2.line(img, (int(x), top + 2), (int(x), top + bh - 2), CLEAT, 2)
                x += 44
        anns = []
        for c in self.cookies:
            cy = self.line_y(c.line)
            poly = c.polygon(cy)
            ipoly = np.round(poly).astype(np.int32)
            shadow = ipoly + np.array([int(c.r * 0.10), int(c.r * 0.16)], np.int32)
            cv2.fillPoly(img, [shadow], (8, 8, 8), cv2.LINE_AA)
            fill = tuple(int(min(255, v * c.tone)) for v in (75, 138, 201))
            cv2.fillPoly(img, [ipoly], fill, cv2.LINE_AA)
            cv2.polylines(img, [ipoly], True, RIM, max(2, int(c.r * 0.13 / 2)), cv2.LINE_AA)
            for (ux, uy, ur) in c.chips:
                xx = c.x + (ux * math.cos(c.rot) - uy * math.sin(c.rot)) * c.r
                yy = cy + (ux * math.sin(c.rot) + uy * math.cos(c.rot)) * c.r
                cv2.circle(img, (int(xx), int(yy)), max(1, int(ur * c.r)), CHIP, -1, cv2.LINE_AA)
            x0, y0 = poly.min(axis=0)
            x1, y1 = poly.max(axis=0)
            anns.append({
                "id": c.id, "line": c.line + 1,
                "category": "deformed" if c.defect else "round",
                "defect_type": c.typ if c.defect else None,
                "roundness": round(min(c.rad) / max(c.rad), 4),
                "diameter_px": c.r * 2,
                "bbox": [round(float(v), 2) for v in (x0, y0, x1 - x0, y1 - y0)],
            })
        if draw_scan:
            sx = int(self.width * SCAN)
            for y in range(0, self.height, 12):
                cv2.line(img, (sx, y), (sx, y + 7), (69, 90, 255), 1)
        return img, anns

    def labels(self, anns: list[dict]) -> dict:
        """시뮬레이터 exportLabels() 와 같은 모양의 정답 문서."""
        return {
            "image": {"width": self.width, "height": self.height, "dpr": 1},
            "params": {"lines": self.lines, "lineGap": self.line_gap, "speed": self.speed, "size": self.size,
                       "gap": self.gap, "defect": self.defect_rate * 100, "strength": self.strength,
                       "scan_line_x": round(self.width * SCAN, 1), "seed": self.seed},
            "counts": {"total_passed": self.stats["total"], "defect_passed": self.stats["ng"],
                       "in_frame": len(anns)},
            "annotations": anns,
        }


def camera_view(canvas: np.ndarray, seed: int = 0, out_size: tuple[int, int] = (1280, 720),
                max_tilt_deg: float = 15.0, hfov_deg: float = 70.0, fill: float = 0.78, noise: float = 4.0
                ) -> tuple[np.ndarray, list[list[float]]]:
    """캔버스를 폰으로 찍은 것처럼 만든다. 반환: (프레임, 캔버스 네 모서리 [좌상,우상,우하,좌하] 0~1 비율).

    캔버스 평면을 3D 에서 기울이고 핀홀 카메라(주점 = 영상 중심, 가로 화각 hfov)로 투영한다.
    실제 폰 촬영과 같은 기하라서 C2 의 가로세로 비율 추정을 정직하게 시험할 수 있다.
    """
    rng = np.random.default_rng(seed)
    w, h = out_size
    ch, cw = canvas.shape[:2]
    f = (w / 2) / math.tan(math.radians(hfov_deg) / 2)
    ax, ay, az = np.radians(rng.uniform(-max_tilt_deg, max_tilt_deg, 2).tolist() + [rng.uniform(-3, 3)])
    rx = np.array([[1, 0, 0], [0, math.cos(ax), -math.sin(ax)], [0, math.sin(ax), math.cos(ax)]])
    ry = np.array([[math.cos(ay), 0, math.sin(ay)], [0, 1, 0], [-math.sin(ay), 0, math.cos(ay)]])
    rz = np.array([[math.cos(az), -math.sin(az), 0], [math.sin(az), math.cos(az), 0], [0, 0, 1]])
    plane = np.array([[-cw / 2, -ch / 2, 0], [cw / 2, -ch / 2, 0], [cw / 2, ch / 2, 0], [-cw / 2, ch / 2, 0]])
    z = f * cw / (fill * w)  # 정면일 때 캔버스가 프레임 폭의 fill 만큼 차지하는 거리
    cam = plane @ (rz @ ry @ rx).T + np.array([0, 0, z])
    dst = np.stack([f * cam[:, 0] / cam[:, 2] + w / 2, f * cam[:, 1] / cam[:, 2] + h / 2], 1).astype(np.float32)
    m = cv2.getPerspectiveTransform(np.array([[0, 0], [cw, 0], [cw, ch], [0, ch]], np.float32), dst)
    frame = np.full((h, w, 3), (38, 40, 44), np.uint8)  # 모니터 베젤/주변
    cv2.warpPerspective(canvas, m, (w, h), dst=frame, borderMode=cv2.BORDER_TRANSPARENT)
    gain = rng.uniform(0.85, 1.15)
    frame = cv2.convertScaleAbs(frame, alpha=gain, beta=rng.uniform(-8, 8))
    frame = cv2.GaussianBlur(frame, (3, 3), 0)
    if noise > 0:
        frame = np.clip(frame.astype(np.float32) + rng.normal(0, noise, frame.shape), 0, 255).astype(np.uint8)
    corners = [[round(float(x) / w, 5), round(float(y) / h, 5)] for x, y in dst]
    return frame, corners


def encode_jpeg(img: np.ndarray, quality: int = 70) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("imencode failed")
    return buf.tobytes()


def resize_long(img: np.ndarray, long_side: int) -> np.ndarray:
    h, w = img.shape[:2]
    s = long_side / max(h, w)
    if abs(s - 1) < 1e-6:
        return img
    return cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
