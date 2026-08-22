FROM python:3.11-slim

# ffmpeg is required by yt-dlp. ca-certificates keeps provider and R2 TLS
# verification independent from the host image.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --gid 10001 neatie && \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin neatie && \
    mkdir -p /var/lib/neatie/runtime /app/downloads && \
    chown -R neatie:neatie /var/lib/neatie /app/downloads

COPY --chown=neatie:neatie . .

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AURALIS_RUNTIME_DIR=/var/lib/neatie/runtime
ENV PORT=8000
EXPOSE 8000

USER neatie

CMD ["python", "-m", "uvicorn", "api_app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
