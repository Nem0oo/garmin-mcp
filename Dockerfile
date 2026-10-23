FROM python:3.12-slim

LABEL org.opencontainers.image.title="garmin-mcp" \
      org.opencontainers.image.description="Read-only MCP server for the official Garmin Connect Personal API"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src /app

RUN useradd --system --no-create-home --uid 10001 app
USER app

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ.get('PORT','8001'),timeout=3)" || exit 1

CMD ["python", "-m", "garmin_mcp"]
