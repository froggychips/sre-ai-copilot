# Base-образ можно (и нужно) пиннить по digest в CI:
#   FROM python:3.11-slim@sha256:<digest>
# Текущая закреплённая версия пакета: 3.11-slim (Debian 12 trixie line).
FROM python:3.11-slim

# kubectl-версия пиннится явно. Обновлять — осознанно, вместе с KUBECTL_SHA256.
# Получить актуальный sha256:
#   curl -fsSL https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256
ARG KUBECTL_VERSION=v1.31.0
ARG KUBECTL_SHA256=""

RUN groupadd -g 10001 sre && \
    useradd -u 10001 -g sre -m -s /bin/bash sre

WORKDIR /app

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates libpq-dev gcc; \
    curl -fsSL -o /tmp/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"; \
    if [ -n "${KUBECTL_SHA256}" ]; then \
        echo "${KUBECTL_SHA256}  /tmp/kubectl" | sha256sum -c -; \
    else \
        curl -fsSL -o /tmp/kubectl.sha256 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256"; \
        echo "$(cat /tmp/kubectl.sha256)  /tmp/kubectl" | sha256sum -c -; \
    fi; \
    install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl; \
    rm -f /tmp/kubectl /tmp/kubectl.sha256

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# gcc нужен только во время сборки колёс, в runtime он лишний attack surface.
RUN apt-get purge -y --auto-remove gcc; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

COPY . .
RUN chown -R sre:sre /app

USER 10001

EXPOSE 8000 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
