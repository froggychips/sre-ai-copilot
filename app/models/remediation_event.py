"""Контракт события внешнего исполнителя (`POST /webhooks/remediation`).

Первый и пока единственный отправитель — squad-medic
(external/mcp, `tools-server/src/squad_medic.py`); формат согласован там же
08.09.2026. Ключи намеренно совпадают с итоговым JSON медика, чтобы у него не
было второго словаря.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

Outcome = Literal["fixed", "partial", "unresolved", "failed", "noop"]


class RemediationEventIn(BaseModel):
    actor: str = Field(..., min_length=1, max_length=64, description="Кто действовал: squad-medic")
    run_id: Optional[str] = Field(None, max_length=200, description="Идентификатор прогона у исполнителя")
    squad: Optional[str] = Field(None, max_length=64)
    namespace: str = Field(..., min_length=1, max_length=253, description="Основной namespace стенда")
    namespaces: List[str] = Field(default_factory=list)
    service_name: Optional[str] = Field(None, max_length=253)
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_min: Optional[int] = Field(None, ge=0)
    outcome: Outcome
    severity: Optional[str] = Field(None, max_length=32)
    fixed: bool = False
    still_unhealthy: bool = False
    applied: List[Any] = Field(default_factory=list)
    manual: List[Any] = Field(default_factory=list)
    gaps: List[Any] = Field(default_factory=list)
    summary: Optional[str] = Field(None, max_length=4000)
    root_cause: Optional[str] = Field(None, max_length=2000)
    next_action: Optional[str] = Field(None, max_length=2000)
    escalated: bool = False
    owner_login: Optional[str] = Field(None, max_length=128)
    extras: Optional[Dict[str, Any]] = None

    @field_validator("namespaces")
    @classmethod
    def _dedupe_namespaces(cls, v: List[str]) -> List[str]:
        seen: List[str] = []
        for ns in v:
            if ns and ns not in seen:
                seen.append(ns)
        return seen[:50]

    @field_validator("applied", "manual", "gaps")
    @classmethod
    def _cap_lists(cls, v: List[Any]) -> List[Any]:
        # Списки идут в JSON-колонку и в timeline; исполнитель с багом не
        # должен уметь положить туда мегабайты.
        return [str(x)[:1000] if not isinstance(x, (dict, list)) else x for x in v[:100]]
