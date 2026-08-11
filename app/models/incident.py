from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class AlertManagerAlert(BaseModel):
    """Single alert as delivered by Prometheus AlertManager webhook v4.

    `status` поле — формально строка ("firing"|"resolved") в webhook v4,
    но некоторые AM-роутеры/прокси (notify_resolver, alertmanager-bot, etc.)
    дополняют payload вложенным объектом вида
    `{"state": "active"|"suppressed", "silencedBy": [...], "inhibitedBy": [...]}`.
    Чтобы не терять signal — принимаем оба формата; если пришёл объект,
    приводим status к "firing"/"resolved" по `state`, а оригинал хранится
    в `status_extra` и доступен из enrichment-кода.
    """

    status: str  # "firing" | "resolved"
    status_extra: Optional[Dict[str, Any]] = None
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: str
    endsAt: Optional[str] = None
    generatorURL: Optional[str] = None
    fingerprint: str

    @model_validator(mode="before")
    @classmethod
    def _extract_status_extra(cls, data: Any) -> Any:
        """Если `status` пришёл объектом — извлекаем state/silenced/inhibited.

        AM webhook v4 spec: `status` — это string. AM API v2 и некоторые
        прокси расширяют payload до `{"state": "...", "silencedBy": [...],
        "inhibitedBy": [...]}`. Чтобы существующий pipeline (status==str)
        не сломался — приводим к string, исходный dict кладём в
        status_extra. Дополнительно — labels-based fallback (`silenced_by`/
        `inhibited_by` keys), который встречается у самописных AM-шлюзов.

        Входной dict НЕ мутируем: при `mode="before"` сюда прилетает ровно тот
        объект, что держит вызывающий (у нас — распарсенное тело AM-вебхука,
        которое дальше уходит в `Incident.raw`, лог и retry). Перезапись
        `data["status"]` на месте меняла payload под чужими руками: повторная
        валидация того же dict-а уже не видела исходный объект-status, а
        сохранённый «сырой» алерт врал про то, что реально пришло от AM.
        Правки собираем в отдельный overlay и возвращаем копию.
        """
        if not isinstance(data, dict):
            return data
        patch: Dict[str, Any] = {}
        status = data.get("status")
        if isinstance(status, dict):
            state = status.get("state")
            patch["status_extra"] = status
            patch["status"] = "resolved" if state == "resolved" else "firing"
        elif data.get("status_extra") is None:
            labels = data.get("labels") or {}
            sb = labels.get("silenced_by")
            ib = labels.get("inhibited_by")
            if sb or ib:
                patch["status_extra"] = {
                    "state": "suppressed",
                    "silencedBy": [sb] if sb else [],
                    "inhibitedBy": [ib] if ib else [],
                }
        if not patch:
            return data
        return {**data, **patch}


class AlertManagerWebhook(BaseModel):
    """Payload shape posted by AlertManager to a webhook receiver."""

    version: str
    groupKey: str
    status: str
    receiver: Optional[str] = None
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: Optional[str] = None
    alerts: List[AlertManagerAlert]


class Incident(BaseModel):
    """Internal canonical incident shape used by the agent pipeline."""

    incident_id: str
    severity: str
    status: str  # "firing" | "resolved"
    summary: str
    description: str = ""
    namespace: Optional[str] = None
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    starts_at: str
    ends_at: Optional[str] = None
    generator_url: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)
    # Заполняется на ingestion из app.services.teamcity_service.incident_teamcity_context.
    # None = TC не сконфигурирован, namespace не маппится, или TC недоступен.
    teamcity_context: Optional[Dict[str, Any]] = None
    # >0 если алерт снова сработал после RESOLVED — счётчик циклов флаппинга.
    flap_count: int = 0
    # AM inhibit/silence state (когда payload пришёл расширенным форматом
    # AM API v2 — `status: {state, silencedBy, inhibitedBy}`). None — обычный
    # active alert. См. AlertManagerAlert._extract_status_extra.
    status_extra: Optional[Dict[str, Any]] = None

    @classmethod
    def from_alertmanager(cls, alert: AlertManagerAlert) -> "Incident":
        labels = alert.labels
        annotations = alert.annotations
        return cls(
            incident_id=alert.fingerprint,
            severity=labels.get("severity", "unknown"),
            status=alert.status,
            summary=annotations.get(
                "summary", labels.get("alertname", "unknown alert")
            ),
            description=annotations.get("description", ""),
            namespace=labels.get("namespace"),
            labels=labels,
            annotations=annotations,
            starts_at=alert.startsAt,
            ends_at=alert.endsAt,
            generator_url=alert.generatorURL,
            raw=alert.model_dump(),
            status_extra=alert.status_extra,
        )


class AgentResponse(BaseModel):
    agent_name: str
    content: str
    metadata: Dict[str, Any] = {}


class FullAnalysisReport(BaseModel):
    incident_id: str
    summary: str
    hypotheses: List[str]
    critic_feedback: str
    suggested_fix: str
    kubectl_commands: List[str] = []
    risk_level: str
    requires_approval: bool
