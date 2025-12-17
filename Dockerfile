FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.sh .
COPY src ./src
COPY config ./config
RUN mkdir -p data logs

EXPOSE 8006
CMD ["./entrypoint.sh"]
