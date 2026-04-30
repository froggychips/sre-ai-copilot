# SRE AI Copilot Architecture

## 1. System Overview
SRE AI Copilot — это детерминированная система каузального анализа инцидентов. В отличие от обычных LLM-агентов, система работает как **Reasoning Engine**, где LLM используется только как инструмент извлечения данных (Extractor) и планирования, а основные выводы строятся на базе **Evidence-Driven Belief Propagation**.

## 2. End-to-End Data Flow

```ascii
Incident -> Ingestion (Webhook)
    -> Evidence Bootstrapper (Context -> Evidence)
    -> EvidenceGraph Construction
    -> Belief Propagation Engine (Signal Propagation)
    -> Bayesian Confidence Calculation
    -> Hypothesis Competition (Ranking)
    -> Interpretation Engine (Deterministic RCA)
    -> Discord Notification / Fix Execution
```

## 3. Reasoning Loop
Система итерирует процесс, пока не достигается сходимость:
1. **Evidence Collection**: Сбор данных из кластера (`logs`, `metrics`, `deployments`).
2. **Belief Propagation**: Сигнализация по причинно-следственному графу (`SUPPORTS`/`CONTRADICTS`).
3. **Stabilization**: Применение `dampening` для предотвращения осцилляций (стабилизация убеждений).
4. **Ranking & Confidence**: Вычисление вероятностей гипотез на основе состояния графа.
5. **Convergence Check**: Проверка дельты состояний. Если система стабильна — генерация финального отчета.
