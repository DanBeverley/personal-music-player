FROM python:3.11-slim

# Install ffmpeg which is strictly required by yt-dlp to extract audio correctly
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Ensure correct permissions
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Providers inject dynamic PORT values
ENV PORT=8000
EXPOSE $PORT

CMD uvicorn api_app:app --host 0.0.0.0 --port $PORT
