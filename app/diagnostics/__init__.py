"""Deterministic diagnostics: правила, выдающие structured Facts.

Назначение: жёсткие, проверяемые наблюдения над enriched context инцидента
(события k8s, recent deploys, метрики) ДО любой LLM-стадии. LLM-агенты
далее работают поверх этих фактов, а не сырого текста alert-а.

Поток:

    enriched_ctx (dict)         # ContextBuilder + k8s_facts + recent deploys
        │
        ▼
    DiagnosticEngine.run(ctx)
        │
        ├─ OOMKilledRule           → Fact(kind="oom_killed", observed=True/False, evidence={...})
        ├─ CrashLoopBackOffRule    → Fact(kind="crashloop", ...)
        ├─ FailedSchedulingRule    → Fact(...)
        ├─ RecentDeployRule        → Fact(kind="recent_deploy", evidence={delta_min, deploy_id})
        ├─ ResourcePressureRule    → Fact(...)
        └─ UpstreamDegradedRule    → Fact(...)
        │
        ▼
    FactStore  ─── to_prompt_context() ──▶  LLM hypothesis-агенты получают
                                            набор подтверждённых фактов,
                                            а не свободный текст.

Дизайн-инвариант: правила НИЧЕГО не интерпретируют («это OOM из-за memory
leak»), они только наблюдают («OOMKilled видели 3 раза за 5 мин»). Это
важно для disagreement-as-signal в multi-agent reasoning поверх.
"""

from app.diagnostics.engine import DiagnosticEngine, default_engine
from app.diagnostics.facts import Fact, FactStore

__all__ = ["Fact", "FactStore", "DiagnosticEngine", "default_engine"]
