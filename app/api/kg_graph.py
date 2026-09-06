"""Читающее API графа: blast radius сервиса с доказательствами.

`GET /kg/blast-radius?namespace=&service=&hops=` → `blast_radius.blast_radius_v2`.
Router-level auth — в app.main (`get_current_user`), как у /replay и /kg/incidents.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.knowledge_graph.blast_radius import (DEFAULT_LIMIT, DEFAULT_MAX_HOPS,
                                              blast_radius_v2)

router = APIRouter()


@router.get("/blast-radius")
def get_blast_radius(
    namespace: str = Query(..., min_length=1),
    service: str = Query(..., min_length=1),
    hops: int = Query(DEFAULT_MAX_HOPS, ge=1, le=4),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=1000),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    result = blast_radius_v2(
        db, namespace, service, max_hops=hops, limit=limit, include_inactive=include_inactive,
    )
    if result.get("target") is None:
        raise HTTPException(status_code=404, detail="service not found in graph")
    return result
