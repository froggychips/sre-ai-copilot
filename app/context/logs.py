"""Сбор pod-логов для LLM-контекста.

PII/секреты: сырые pod-логи уходили в context_builder → промпт LLM без
редакции — модуль `pii_redaction` применялся только на пути Seq→Discord.
Логи приложений штатно содержат Bearer-токены, connection-string-и с
паролями, email-ы юзеров и pod-IP. Редактируем на ГРАНИЦЕ СБОРА, до того
как текст попадёт в контекст: любой downstream-потребитель (LLM, embed,
KG) получает уже обезличенный текст.
"""
from kubernetes import client

from app.services.pii_redaction import redact_pii

# Хвост, который отдаём модели. Режем ПОСЛЕ редакции: если сначала обрезать,
# паттерны увидят половину токена и не сматчат её (утечка обрубка секрета).
_TAIL_CHARS = 500


class LogCollector:
    def get_summary(self, namespace: str, pod_name: str) -> str:
        v1 = client.CoreV1Api()
        # Берем последние 50 строк
        try:
            logs = v1.read_namespaced_pod_log(
                name=pod_name, namespace=namespace, tail_lines=50
            )
            # max_len=None — усечение здесь своё (хвост, а не голова):
            # диагностика в логах живёт в последних строках.
            return redact_pii(logs, max_len=None)[-_TAIL_CHARS:]
        except Exception as e:
            return f"Could not fetch logs: {str(e)}"
