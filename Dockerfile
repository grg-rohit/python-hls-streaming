#python-hls-streaming\Dockerfile
# Lightweight API image; FFmpeg, PyTorch and FrameLift stay in worker images.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY tools/ ./tools/
COPY public/ ./public/
COPY run.py ./run.py
EXPOSE 8000
CMD ["sh", "-c", "exec python run.py --host 0.0.0.0 --port ${PORT:-8000}"]

#CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
