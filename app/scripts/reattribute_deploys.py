"""Переатрибуция kg_deployments по реальной цели билдов TeamCity.

Записи, сделанные до фикса атрибуции, привязаны к namespace по VCS-ветке —
у deploy-конфигов она литеральный `<default>`, и нормализация в `preprod`
была догадкой. Настоящая цель лежит в параметрах билда (`NAMESPACE`,
`SERVICE_NAME`); модуль `app.knowledge_graph.deploy_reattribution`
дозапрашивает их у TC и приводит записи в соответствие.

Билды, для которых TC не отдал параметры (выпали из retention), не
трогаются: правильная цель неизвестна, а удалять по незнанию нельзя.

CLI:
    python -m app.scripts.reattribute_deploys                    # отчёт
    python -m app.scripts.reattribute_deploys --apply            # применить
    python -m app.scripts.reattribute_deploys --limit-builds 20  # прогон на части
"""
import argparse
import json
import sys

from app.database import SessionLocal
from app.knowledge_graph.deploy_reattribution import reattribute_deployments


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="реально переписать (без флага — dry-run)")
    parser.add_argument("--limit-builds", type=int, default=None,
                        help="обработать только первые N билдов (для проверки)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = reattribute_deployments(
            db, apply=args.apply, limit_builds=args.limit_builds,
        )
    finally:
        db.close()

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if not args.apply and (result.get("rows_deleted") or result.get("rows_created")):
        print(
            f"\nℹ️  dry-run: удалить {result['rows_deleted']}, "
            f"создать {result['rows_created']}; билдов без ответа TC "
            f"{result['builds_unknown']}. Запусти с --apply.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
