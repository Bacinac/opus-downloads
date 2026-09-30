FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# yt-dlp merges separate video and audio streams
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY requirements.lock ./
RUN uv pip install --system --require-hashes -r requirements.lock
RUN --mount=type=bind,from=plugins,target=/plugins \
    for lock in /plugins/*/requirements/opus-downloads.lock; do \
        [ ! -f "$lock" ] || uv pip install --system --require-hashes -r "$lock"; \
    done

COPY . .

ENV PYTHONDONTWRITEBYTECODE=1

CMD ["./entrypoint.sh"]
