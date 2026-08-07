FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RINGBEARER_STATE_DIR=/data \
    BIND_HOST=0.0.0.0 \
    BIND_PORT=8787

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ringbearer.py .env.example ./

RUN useradd --create-home --uid 1000 ringbearer \
    && mkdir /data \
    && chown ringbearer:ringbearer /data

USER ringbearer
EXPOSE 8787

ENTRYPOINT ["python", "/app/ringbearer.py"]
