# Dockerfile
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# install minimal system deps (ping)
RUN apt-get update && \
    apt-get install -y --no-install-recommends iputils-ping gcc build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# make entrypoint executable
RUN chmod +x /app/entrypoint.sh || true

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["--once"]
