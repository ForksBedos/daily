# UzDigest в контейнере: бот работает постоянно, сбор и сборку запускает Планировщик через docker compose exec.
# Код и настройки подключаются папками (docker-compose.yml): правка подхватывается без пересборки образа.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Tashkent

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app
COPY pyproject.toml docker-constraints.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c docker-constraints.txt -e ".[dev]"
COPY config ./config

# Том наследует владельца этой папки: иначе база была бы недоступна на запись
RUN mkdir -p data/backup logs outputs && chown -R app:app /app
USER app

CMD ["python", "-m", "uzdigest", "bot"]
