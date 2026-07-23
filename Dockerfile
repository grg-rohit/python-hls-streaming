# ── Base image: Python 3.11 slim + FFmpeg ─────────────────────────────────
FROM python:3.11-slim

# Install FFmpeg from Debian repos (no Python FFmpeg wrapper needed)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# ── Working directory ──────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ─────────────────────────────────────────────────────
COPY app/       ./app/
COPY public/    ./public/
COPY convert_video.py .
COPY run.py .

# Create directories that must exist at runtime
# (videos/ and streams/ are bind-mounted from the host via docker-compose,
#  but we create them here so the image works standalone too)
RUN mkdir -p videos streams

# ── Runtime ────────────────────────────────────────────────────────────────
EXPOSE 8000

# Default command: start the streaming server.
# To run conversion instead, override with:
#   docker compose run --rm converter
CMD ["python", "run.py"]
