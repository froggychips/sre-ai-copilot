# Module Documentation

## Core Reasoning
- `app/core/reasoning/evidence.py`: Базовые примитивы графа.
  - `Evidence`: Типизированный факт с `freshness_score`.
  - `EvidenceRelation`: Каузальные связи (SUPPORTS, CONTRADICTS, CAUSES).
  - `EvidenceGraph`: Хранилище узлов и связей.
- `app/core/reasoning/hypothesis.py`: Определение гипотез через ID доказательств.
- `app/core/reasoning/reasoning_state.py`: Состояние рассуждений (стек итераций, история).

## Dynamics & Engine
- `app/core/reasoning/dynamics/belief_propagation_engine.py`: Итеративное распространение убеждений.
- `app/core/reasoning/dynamics/stabilization.py`: Слой демпфирования сигналов.
- `app/core/reasoning/engine/interpretation.py`: Генератор RCA-отчета и каузальной цепочки.

## Services
- `app/services/session_manager.py`: Redis-хранилище сессий.
- `app/services/k8s_guard.py`: Валидатор безопасности команд (Guardrails).
- `app/services/resilience.py`: Ретраи (Tenacity) и Circuit Breakers.
