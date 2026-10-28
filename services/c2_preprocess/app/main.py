"""C2 Preprocess — 디코드, ROI 원근 보정, 리사이즈, 재인코딩. 무상태.

명세: docs/02-containers.md#c2-preprocess, 메시지: docs/03-messages.md §4.1
"""
import os

from fastapi import FastAPI, HTTPException, Request

app = FastAPI(title="c2-preprocess")

TARGET_WIDTH = int(os.getenv("TARGET_WIDTH", "640"))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "c2-preprocess"}


@app.post("/preprocess")
async def preprocess(request: Request):
    # TODO: body(JPEG) → cv2.imdecode (실패 422 DECODE_FAILED)
    #       X-Roi 있으면 getPerspectiveTransform + warpPerspective (형식 오류 400 BAD_ROI)
    #       긴 변 X-Target-Width|TARGET_WIDTH 로 리사이즈 → JPEG(q=85)
    #       응답 헤더: X-Width, X-Height, X-Roi-Applied, X-Proc-Ms
    raise HTTPException(501, "TODO")
