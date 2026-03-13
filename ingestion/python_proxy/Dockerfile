FROM python:3.11-slim

# Install ffmpeg which is strictly required by yt-dlp to extract audio correctly
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all the proxy code
COPY . .

# Railway provides the PORT environment variable dynamically
ENV PORT=8080
EXPOSE $PORT

# Start the uvicorn server
CMD uvicorn server:app --host 0.0.0.0 --port $PORT
