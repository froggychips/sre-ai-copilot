# SRE AI Copilot

**SRE AI Copilot** — это детерминированный движок каузального анализа инцидентов (Reasoning Engine), предназначенный для автоматизации процессов SRE в Kubernetes.

## 🧠 Архитектура
Система не является чат-ботом. Это **Reasoning Engine**, работающий на основе **Evidence-Driven Belief Propagation**.
1. **Ingestion**: Прием инцидентов (New Relic) и их преобразование в типизированные факты (Evidence).
2. **Reasoning Core**: Построение причинно-следственного графа и распространение вероятностей (beliefs) по связям.
3. **Interpretation**: Детерминированное построение RCA-отчета на основе графовых связей.

## 🚀 Quick Start

### Требования
- Docker, Docker Compose
- Python 3.11+
- Helm / kubectl

### Настройка
1. Скопируйте `.env.example` в `.env` и укажите необходимые ключи:
   - `GEMINI_API_KEY`: Ключ API для ИИ.
   - `DATABASE_URL`: Строка подключения к PostgreSQL.
   - `REDIS_URL`: URL для Redis.
   - `DISCORD_WEBHOOK_URL`: Для уведомлений об инцидентах.
   - `JWT_PUBLIC_KEY`: RSA ключ для валидации запросов.

### Запуск
```bash
# Инфраструктура
docker-compose up -d

# Деплой в Kubernetes
helm install copilot ./charts/copilot
```

## 🧪 Тестирование
Система детерминирована. Тесты мокируют внешние API, чтобы проверять правильность графовых преобразований.
```bash
pip install -r requirements.txt
pytest tests/
```

## 🛡 Безопасность (Guardrails)
- **K8s DSL**: Агент не исполняет команды напрямую, он генерирует `ExecutionIntent` (DSL).
- **Prompt Isolation**: Изоляция ввода через `<user_context>` XML-теги.
- **Human-in-the-Loop (HITL)**: Критические операции требуют подтверждения в Discord.

## 📚 Документация
- [Architecture](docs/ARCHITECTURE.md) — Потоки данных и Reasoning Loop.
- [Module Docs](docs/MODULE_DOCS.md) — Справочник модулей.
- [DR Plan](docs/DR.md) — Disaster Recovery и бэкапы.

## 📊 Observability
Система поддерживает:
- **Metrics**: Эндпоинт `/metrics` (Prometheus).
- **Tracing**: OpenTelemetry (OTLP) экспорт в Jaeger/Tempo.
- **Logs**: Структурированные JSON-логи (structlog).
