from app.core.intelligence.blast_radius import BlastRadiusEngine


class RCAExplainer:
    """Детерминированный генератор RCA-репорта.

    Гейт аппрува здесь — security-критичен: неузнанная причина ОБЯЗАНА
    требовать ручного аппрува (fail-safe), а не проскакивать как auto-approvable
    только потому, что человекочитаемая строка `name` дрейфанула.
    """

    # Действия маппятся по СТАБИЛЬНОМУ ключу `kind` (slug), а не по
    # человекочитаемому `name`. Дрейф display-строки не должен ронять
    # security-гейт.
    ACTION_MAP = {
        "oom": {
            "command": "kubectl set resources deployment <name> --limits=memory=...",
            "risk": "MEDIUM",
            "reason": "Safe but changes resource quotas",
        },
        "app_crash": {
            "command": "kubectl rollout restart deployment <name>",
            "risk": "SAFE",
            "reason": "Standard stateless restart",
        },
    }

    # Нормализация человекочитаемых имён в стабильный kind — мост совместимости
    # для гипотез без явного `kind`. Сопоставление по подстроке (lowercase),
    # чтобы пережить мелкий дрейф формулировок.
    _NAME_TO_KIND = (
        (("memory", "oom"), "oom"),
        (("runtime", "crash", "panic"), "app_crash"),
    )

    @classmethod
    def _resolve_kind(cls, hyp):
        """Достаёт стабильный kind из гипотезы.

        Приоритет: явное поле `kind` → нормализация из `name` → None
        (неузнанная причина). None намеренно ведёт к fail-safe в вызывающем коде.
        """
        kind = hyp.get("kind")
        if kind:
            return str(kind).strip().lower()
        name = str(hyp.get("name", "")).lower()
        for needles, mapped in cls._NAME_TO_KIND:
            if any(n in name for n in needles):
                return mapped
        return None

    @classmethod
    def create_report(cls, incident_id, hypotheses, graph=None, topology=None):
        """
        Creates a strict, production-grade SRE RCA report.
        """
        if not hypotheses:
            # Безопасный дефолт-репорт, не краш. Ничего не предлагаем,
            # ничего не авто-аппрувим.
            return {
                "incident_id": incident_id,
                "summary": "No root cause identified.",
                "hypotheses": [],
                "root_cause": None,
                "temporal_diff": [],
                "blast_radius": {"critical": [], "affected_pods": 0, "severity": "LOW"},
                "suggested_actions": [],
                "risk_level": "LOW",
                "approval_required": False,
            }

        # 1. Select Root Cause (Highest Confidence) — без жёсткого индекса по ключу.
        root_cause_obj = max(hypotheses, key=lambda x: x.get("confidence", 0.0))
        root_confidence = root_cause_obj.get("confidence", 0.0)
        root_name = root_cause_obj.get("name", "Unknown")
        root_kind = cls._resolve_kind(root_cause_obj)

        # 2. Blast Radius Calculation
        blast_info = BlastRadiusEngine.calculate(
            {"targets": [{"service": root_name}]}, topology or {}
        )

        # 3. Action Safety Layer (Deterministic) — маппинг по стабильному kind.
        suggested_actions = []
        action = cls.ACTION_MAP.get(root_kind) if root_kind else None
        if action:
            suggested_actions.append(dict(action))

        # FAIL-SAFE: неузнанная причина → принудительный аппрув и ненизкий риск.
        # Нельзя занижать риск только из-за того, что строку не распознали.
        unknown_root_cause = root_kind is None or not suggested_actions

        # 4. Temporal Diff
        t_diff = []
        if graph:
            # Simplified logic for practical report
            t_diff = [
                "Deployment update detected",
                "Traffic spike observed",
            ]  # Placeholder

        actions_require_approval = any(
            a["risk"] in ["MEDIUM", "HIGH", "CRITICAL"] for a in suggested_actions
        )

        risk_level = (
            "MEDIUM"
            if (
                unknown_root_cause
                or any(a["risk"] == "MEDIUM" for a in suggested_actions)
            )
            else "LOW"
        )

        return {
            "incident_id": incident_id,
            "summary": (
                f"Incident in {blast_info['affected_service']} detected with "
                f"{round(root_confidence * 100, 1)}% confidence."
            ),
            "hypotheses": [
                {
                    "name": h.get("name", "Unknown"),
                    "confidence": h.get("confidence", 0.0),
                    "evidence": h.get("evidence", []),
                }
                for h in hypotheses
            ],
            "root_cause": {
                "name": root_name,
                "confidence": root_confidence,
            },
            "temporal_diff": t_diff,
            "blast_radius": {
                "critical": [blast_info["affected_service"]],
                "affected_pods": blast_info["pods_count"],
                "severity": blast_info["severity"].upper(),
            },
            "suggested_actions": suggested_actions,
            "risk_level": risk_level,
            # FAIL-SAFE: аппрув обязателен, если действия его требуют ЛИБО
            # причина неузнана.
            "approval_required": actions_require_approval or unknown_root_cause,
        }
