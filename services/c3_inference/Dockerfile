FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app SAM2_BUILD_CUDA=0
ARG WITH_SAM2=0
COPY services/c3_inference/requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# SAM 2 (CPU): WITH_SAM2=1 로 빌드할 때만 설치 — 이미지가 커지므로 기본은 끔
RUN if [ "$WITH_SAM2" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* && \
      pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu && \
      pip install --no-cache-dir -r requirements-sam2.txt ; \
    fi
COPY shared ./shared
COPY services/c3_inference/app ./app
EXPOSE 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
