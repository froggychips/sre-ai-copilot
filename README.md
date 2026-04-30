# SRE AI Copilot

Production-ready Kubernetes incident response platform.

## Architecture
Система является **Deterministic Reasoning Engine**, а не LLM-чатом.
1. **EvidenceGraph**: Причинно-следственный граф фактов.
2. **Belief Propagation**: Итеративный движок распространения сигналов вероятности.
3. **Interpretation Layer**: Детерминированная генерация RCA.

## Documentation
- `docs/ARCHITECTURE.md`: Общая архитектура и потоки данных.
- `docs/MODULE_DOCS.md`: Описание API модулей.
- `docs/DR.md`: Disaster Recovery и Backups.

## Quick Start
```bash
docker-compose up -d
helm install copilot ./charts/copilot
```

## Safety & Governance
- **Guardrails:** Блокировка деструктивных действий.
- **Evidence-Driven:** ИИ не галлюцинирует, он оперирует графом фактов.
- **Explainable RCA:** Каждое решение подтверждено каузальной цепочкой из графа.
