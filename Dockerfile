FROM python:3.11-slim

RUN groupadd -g 10001 sre && \
    useradd -u 10001 -g sre -m -s /bin/bash sre

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl libpq-dev gcc \
    && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
    && rm kubectl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R sre:sre /app

USER 10001

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
