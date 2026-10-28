"""C3 Inference — 과자 마스크 + 원형도/볼록도 판정. 무상태, 복제 가능.

명세: docs/02-containers.md#c3-inference--담당-b, 로직: docs/05-detection.md
메시지: docs/03-messages.md §4.2
"""
import os
import socket

from fastapi import FastAPI, HTTPException, Request

from shared.schemas import InferResult  # noqa: F401

app = FastAPI(title="c3-inference")

REPLICA = socket.gethostname()
C3_BACKEND = os.getenv("C3_BACKEND", "fast")
SAM2_ENABLED = os.getenv("SAM2_ENABLED", "0") == "1"
CIRC_TH = float(os.getenv("CIRC_TH", "0.80"))
SOLID_TH = float(os.getenv("SOLID_TH", "0.95"))


@app.on_event("startup")
async def load_models():
    # TODO: SAM2_ENABLED 이면 backends.sam2_backend 로드, torch.set_num_threads(OMP_NUM_THREADS)
    pass


@app.get("/health")
async def health():
    return {"status": "ok", "service": "c3-inference", "replica": REPLICA,
            "backend": C3_BACKEND, "sam2": SAM2_ENABLED}


@app.post("/infer", response_model=InferResult)
async def infer(request: Request):
    # TODO: X-Mode(mock|fast|precise) 로 백엔드 선택
    #       mock    → backends/mock_backend.py
    #       fast    → backends/opencv_backend.py
    #       precise → opencv bbox → backends/sam2_backend.py (미로드 시 409 PRECISE_UNAVAILABLE)
    #       X-Circ-Th / X-Solid-Th 로 임계값 덮어쓰기, partial 객체는 defect=None
    raise HTTPException(501, "TODO")
