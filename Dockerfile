FROM python:3.12-slim

# ffmpeg merges video + audio and makes MP3s
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# JavaScript runtime that yt-dlp uses to read YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# One worker + threads: download jobs are tracked in memory
CMD ["sh", "-c", "gunicorn app:app --workers 1 --threads 8 --timeout 300 --bind 0.0.0.0:${PORT:-8000}"]
