FROM python:3.11-slim
LABEL maintainer="IT Infrastructure - Digiserve" version="4.0"

RUN apt-get update && apt-get install -y --no-install-recommends curl tzdata && rm -rf /var/lib/apt/lists/*

ENV TZ=Asia/Jakarta
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY config.json .

ARG APP_UID=1000
ARG APP_GID=1000

RUN groupadd -g ${APP_GID} appuser \
    && useradd -m -u ${APP_UID} -g ${APP_GID} -s /usr/sbin/nologin appuser \
    && mkdir -p /backup/sharepoint/.manifests /app/logs \
    && chown -R ${APP_UID}:${APP_GID} /app /backup/sharepoint

USER appuser

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "3", "--timeout", "300", "app.main:app"]
