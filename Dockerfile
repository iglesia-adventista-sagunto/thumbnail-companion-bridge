FROM python:3.11-slim

WORKDIR /app

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/app/config.json

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files and static assets
COPY app.py script.py youtube_service.py config.template.json miniaturas.csv ./
COPY segoe-ui-black.ttf segoe-ui-bold.ttf ./
COPY assets/ ./assets/
COPY templates/ ./templates/

RUN mkdir -p /app/thumbnails

# Healthcheck to verify the HTTP server is healthy and responding
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/health')" || exit 1

EXPOSE 5000

# 1 worker with multiple threads ensures synchronized access to rendering & state
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:5000", "app:app"]
