from prometheus_client import Counter, Histogram, Summary

# --- LLM usage & performance ---------------------------------------------
LLM_LATENCY = Histogram("llm_request_duration_seconds", "LLM API latency", ["model"])
TOKEN_USAGE = Counter("llm_tokens_total", "Total tokens used", ["model", "type"])
API_ERRORS = Counter("llm_api_errors_total", "API error count", ["model", "error_type"])

# --- Pipeline stage latency -----------------------------------------------
#
# Основные стадии — из StageTimer (analyzer / diagnostics / hypothesis /
# critic / fix / risk / synthesis). Дополнительно llm_critic — одна
# LLM-критика на гипотезу (sub-stage внутри critic).
# Сравнение critic vs llm_critic показывает overhead и отвечает на вопрос
# «нужен ли LLM-критик, если algo ловит 90%».
PIPELINE_STAGE_DURATION = Histogram(
    "pipeline_stage_duration_seconds",
    "Latency per pipeline stage",
    ["stage"],
)


def track_llm_metrics(
    model: str, latency: float, prompt_tokens: int, completion_tokens: int
):
    LLM_LATENCY.labels(model=model).observe(latency)
    TOKEN_USAGE.labels(model=model, type="prompt").inc(prompt_tokens)
    TOKEN_USAGE.labels(model=model, type="completion").inc(completion_tokens)


# --- Fact-anchored reasoning pipeline ------------------------------------
#
# Эти метрики дают наблюдаемость над поведением слоёв A/C/D из плана
# архитектуры. Назначение каждой см. в комментариях к Counter-ам.
# Алерты на эти метрики живут в k8s/prometheus-rules.yaml (TODO: добавить).

# Сколько раз каждое diagnostic-правило выдало вердикт. Label `observed` —
# "true"/"false". Если для какого-то kind мы постоянно видим observed=false —
# правило ничего не ловит, надо смотреть на регексы или enrichment-контекст.
FACTS_OBSERVED = Counter(
    "diagnostic_facts_observed_total",
    "Diagnostic facts emitted, by kind and observation status",
    ["kind", "observed"],
)

# Сколько гипотез сгенерировал каждый perspective-агент ДО anchor-фильтра.
# Разница с hypothesis_grounded_total{perspective} показывает, сколько
# отсекает filter_grounded: если perspective генерирует много, но до
# grounded доходит мало — проблема в anchor-дисциплине LLM или в бедности
# observed-фактов для этой perspective.
HYPOTHESES_GENERATED = Counter(
    "hypothesis_generated_total",
    "Hypotheses produced by each perspective before filter_grounded",
    ["perspective"],
)

# Сколько гипотез на perspective прошло anchor-валидацию
# (filter_grounded в HypothesisSet). Если какая-то perspective стабильно
# выдаёт 0 — её промпт-роль не работает на нашем потоке alert-ов.
HYPOTHESES_GROUNDED = Counter(
    "hypothesis_grounded_total",
    "Hypotheses surviving filter_grounded, by perspective",
    ["perspective"],
)

# Сколько гипотез отбраковал критик. Label `by`:
#   "algo" — алгоритмическая проверка (low-conf anchor, unobserved kind)
#   "llm"  — LLM-стадия adversarial-критика
# Большое преобладание algo над llm = LLM-критик можно отключить и
# экономить токены. Преобладание llm = алгоритмическая часть недостаточна.
HYPOTHESES_REFUTED = Counter(
    "hypothesis_refuted_total",
    "Hypotheses refuted by FactCriticAgent, by refutation branch",
    ["by"],
)

# Сколько pipeline-прогонов завершились disagreement_signal != None —
# полезный сигнал, что перспективы реально диверсифицируются. Если всегда
# 0 — perspective-роли сливаются в один prior (mode collapse).
HYPOTHESES_DISAGREEMENT = Counter(
    "hypothesis_disagreement_total",
    "Pipeline runs where perspectives produced disagreeing top hypotheses",
)

# Сколько pipeline-прогонов завершились без выживших гипотез
# (best_candidate is None → "manual triage required"). Высокая частота:
# либо diagnostic-слой слишком слабый, либо anchor-валидация слишком строгая.
PIPELINE_NO_SURVIVOR = Counter(
    "pipeline_no_survivor_total",
    "Pipeline runs where all hypotheses were refuted",
)


def track_fact_observed(kind: str, observed: bool) -> None:
    FACTS_OBSERVED.labels(kind=kind, observed=str(observed).lower()).inc()


def track_grounded(perspective: str) -> None:
    HYPOTHESES_GROUNDED.labels(perspective=perspective).inc()


def track_refuted(branch: str) -> None:
    """branch ∈ {"algo", "llm"}."""
    HYPOTHESES_REFUTED.labels(by=branch).inc()


def track_disagreement() -> None:
    HYPOTHESES_DISAGREEMENT.inc()


def track_no_survivor() -> None:
    PIPELINE_NO_SURVIVOR.inc()


def track_generated(perspective: str) -> None:
    HYPOTHESES_GENERATED.labels(perspective=perspective).inc()


def track_stage_duration(stage: str, duration_s: float) -> None:
    PIPELINE_STAGE_DURATION.labels(stage=stage).observe(duration_s)
