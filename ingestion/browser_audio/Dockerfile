FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
RUN pip install --no-cache-dir playwright==1.55.0 fastapi==0.116.1 uvicorn==0.35.0
WORKDIR /service
COPY service.py .
ENV PYTHONUNBUFFERED=1
CMD ["xvfb-run", "-a", "uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8020", "--no-access-log"]
