# MetrIQ — Legal Metrology Label Scanner
# Built for Hugging Face Spaces (Docker SDK), free CPU tier.

FROM python:3.10-slim

# OpenCV / PaddleOCR need these system libs even in "headless" mode.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so Docker caches this layer between rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Project layout expected by server.py ---
# /app/backend/server.py  (and the other .py modules)
# /app/frontend/index.html, style.css, app.js, etc.
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/

# HF Spaces' Docker SDK routes traffic to port 7860 by default.
# Anyone can write to HOME (needed for HF's model-cache dirs and PaddleOCR's own cache).
ENV HOME=/app \
    PORT=7860
RUN mkdir -p /app/backend/sessions && chmod -R 777 /app

WORKDIR /app/backend
EXPOSE 7860

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "7860"]
