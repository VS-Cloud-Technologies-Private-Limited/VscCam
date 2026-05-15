FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY config.py server.py discover_rtsp_cameras.py ./
COPY static/ static/

RUN mkdir -p hls

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status')" || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
