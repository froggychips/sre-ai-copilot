from prometheus_client import Counter, Histogram

# --- LLM usage & performance ---------------------------------------------
LLM_LATENCY = Histogram("llm_request_duration_seconds", "LLM API latency", ["model"])
TOKEN_USAGE = Counter("llm_tokens_total", "Total tokens used", ["model", "type"])
API_ERRORS = Counter("llm_api_errors_total", "API error count", ["model", "error_type"])

# Per-agent token attribution: какой агент сколько тратит. Label `direction`:
#   "input"  — prompt tokens (то что отправили модели)
#   "output" — completion tokens (то что модель сгенерила)
# Этот counter позволяет видеть кто из 6 агентов жжёт больше (Analyzer,
# Hypothesis, FactCritic, Fix, Risk, Synthesis), и сравнивать стоимость
# до/после prompt-оптимизаций. Bill = sum(input × $/in) + sum(output × $/out).
LLM_TOKENS_PER_AGENT = Counter(
    "llm_tokens_per_agent_total",
    "Tokens used by each agent (real numbers from provider response, not char-approx)",
    ["agent", "model", "direction"],
)

# Per-agent latency — для понимания где узкое место в reasoning chain.
LLM_LATENCY_PER_AGENT = Histogram(
    "llm_latency_per_agent_seconds",
    "LLM round-trip latency by agent",
    ["agent", "model"],
)

# Per-agent error rate. Полезно когда retry-strategy скрывает шум:
# по labels можно увидеть что Synthesis даёт rate_limit чаще чем Analyzer.
LLM_ERRORS_PER_AGENT = Counter(
    "llm_errors_per_agent_total",
    "LLM call errors by agent",
    ["agent", "model", "error_type"],
)

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


def track_llm_usage_per_agent(
    *,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_s: float,
    error_type: str | None = None,
) -> None:
    """Per-agent attribution of LLM cost. Вызывается из BaseAgent.ask()
    с реальными числами из provider-response (не char-approximation)."""
    LLM_LATENCY_PER_AGENT.labels(agent=agent, model=model).observe(latency_s)
    if input_tokens > 0:
        LLM_TOKENS_PER_AGENT.labels(
            agent=agent, model=model, direction="input"
        ).inc(input_tokens)
    if output_tokens > 0:
        LLM_TOKENS_PER_AGENT.labels(
            agent=agent, model=model, direction="output"
        ).inc(output_tokens)
    if error_type:
        LLM_ERRORS_PER_AGENT.labels(
            agent=agent, model=model, error_type=error_type
        ).inc()


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

# Evidence-контракт: found / absent / unknown. Отдельный счётчик, а не
# третий лейбл у FACTS_OBSERVED — у того два значения observed и дашборды
# на них завязаны. Доля unknown по kind — прямой измеритель Known Unknowns:
# сколько проверок копилот не смог выполнить.
FACTS_VERDICT = Counter(
    "diagnostic_facts_verdict_total",
    "Diagnostic facts emitted, by kind and evidence verdict (found/absent/unknown)",
    ["kind", "verdict"],
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

# ── Pipeline quality metrics (Grok review #7) ──────────────────────────────
#
# Эти счётчики формируют картину «что делает копайлот за неделю/месяц» —
# показатели, на которые можно вешать SLO и тюнить prompts/agents:
#   - resolution_quality_total — итоговая раскладка resolved/unresolved
#   - execution_intent_total{parsed} — FixAgent даёт structured-output?
#   - executor_status_total — dry_run-результаты (ok/failed/guard-blocked)
#   - executor_applied_total{success} — реальные kubectl-write outcomes
#   - recurrence_total / flapping_total — частота повторов
#   - hypotheses_count / survivors_count — distribution числа гипотез/выживших

PIPELINE_FACT_CONFLICTS = Counter(
    "pipeline_fact_conflicts_total",
    "Pipeline runs where MUTUALLY_EXCLUSIVE_PAIRS обе observed=True",
    ["kind_a", "kind_b"],
)

PIPELINE_RESOLUTION_QUALITY = Counter(
    "pipeline_resolution_quality_total",
    "Pipeline outcomes by quality category (resolved/unresolved/wrong_apply/error)",
    ["quality"],
)

# parsed=true/false — смог ли FixAgent выдать structured ExecutionIntent.
# action="restart_deployment"/"scale_deployment"/"get_logs"/etc. → "none" если не parsed.
EXECUTION_INTENT_EMITTED = Counter(
    "pipeline_execution_intent_total",
    "Number of pipeline runs where FixAgent emitted ExecutionIntent",
    ["parsed", "action"],
)

EXECUTOR_STATUS = Counter(
    "pipeline_executor_status_total",
    "Outcomes of the executor stage (dry-run server-side validation)",
    ["status"],
)

EXECUTOR_APPLIED = Counter(
    "pipeline_executor_applied_total",
    "Real kubectl apply outcomes (post-approval)",
    ["success"],
)

INCIDENT_RECURRENCE = Counter(
    "pipeline_incident_recurrence_total",
    "Pipeline runs marked as recurring (same service resolved < 7d)",
    ["is_recurrence"],
)

INCIDENT_FLAPPING = Counter(
    "pipeline_incident_flapping_total",
    "Pipeline runs marked as flapping (fired→resolved→fired)",
)

HYPOTHESES_COUNT_PER_RUN = Histogram(
    "pipeline_hypotheses_count",
    "How many hypotheses each pipeline run generated (across all perspectives)",
    buckets=(0, 1, 2, 3, 4, 5, 7, 10, 15, float("inf")),
)

SURVIVORS_COUNT_PER_RUN = Histogram(
    "pipeline_survivors_count",
    "How many hypotheses survived FactCritic adversarial grounding",
    buckets=(0, 1, 2, 3, 5, float("inf")),
)


def track_fact_observed(kind: str, observed: bool) -> None:
    FACTS_OBSERVED.labels(kind=kind, observed=str(observed).lower()).inc()


def track_fact_verdict(kind: str, verdict: str) -> None:
    FACTS_VERDICT.labels(kind=kind, verdict=verdict).inc()


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


# ── Pipeline-quality track helpers (Grok review #7) ────────────────────────

def track_fact_conflict(kind_a: str, kind_b: str) -> None:
    """Один конфликт = один inc для нормализованной пары (sorted)."""
    a, b = sorted([kind_a, kind_b])
    PIPELINE_FACT_CONFLICTS.labels(kind_a=a, kind_b=b).inc()


def track_resolution_quality(quality: str) -> None:
    """quality ∈ {"resolved", "unresolved", "suppressed", "wrong_apply", "error"}.

    "suppressed" — rollout-noise short-circuit: LLM-стадии пропущены,
    инцидент закрыт как шум (см. pipeline._finalize_suppressed).
    """
    PIPELINE_RESOLUTION_QUALITY.labels(quality=quality).inc()


def track_execution_intent(parsed: bool, action: str = "none") -> None:
    """parsed=False → action='none'. Иначе action = ExecutionIntent.action.value."""
    EXECUTION_INTENT_EMITTED.labels(
        parsed=str(parsed).lower(), action=action if parsed else "none"
    ).inc()


def track_executor_status(status: str) -> None:
    """status ∈ {dry_run_ok, dry_run_failed, guardrail_blocked, error, skipped}."""
    EXECUTOR_STATUS.labels(status=status).inc()


def track_executor_applied(success: bool) -> None:
    EXECUTOR_APPLIED.labels(success=str(success).lower()).inc()


def track_incident_recurrence(is_recurrence: bool) -> None:
    INCIDENT_RECURRENCE.labels(is_recurrence=str(is_recurrence).lower()).inc()


def track_incident_flapping() -> None:
    INCIDENT_FLAPPING.inc()


def track_hypotheses_count(n: int) -> None:
    HYPOTHESES_COUNT_PER_RUN.observe(n)


def track_survivors_count(n: int) -> None:
    SURVIVORS_COUNT_PER_RUN.observe(n)
