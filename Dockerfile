FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir ".[api,postgres]" \
    && useradd --create-home --uid 10001 asof

USER asof

EXPOSE 8000

CMD ["python", "-m", "asof.run_server"]
