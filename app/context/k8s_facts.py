from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog
from kubernetes import client
from kubernetes import config as k8s_config

from app.diagnostics.rules.base import same_workload
from app.services.pii_redaction import redact_pii

logger = structlog.get_logger()

# Длина хвоста terminated.message в blob-е (как было до редакции).
_TERMINATED_MSG_LEN = 200


@dataclass
class K8sSnapshot:
    """Structured k8s data для diagnostic rules.

    text — человекочитаемый blob (идёт в logs_summary / LLM context).
        Весь текст, взятый из вывода приложений (pod-логи, terminated.message),
        прогнан через redact_pii: blob уезжает в LLM-промпт, а pod-логи
        штатно содержат Bearer-токены, пароли в connection-string-ах,
        email-ы юзеров и pod-IP.
        Строки про terminated-контейнеры и сами логи скоупятся по
        target-workload (см. _collect_sync): текст ЧУЖИХ подов namespace-а
        не должен матчиться text-fallback-ами правил (OOMKilledRule и др.).
    container_terminated — pod_name → {reason, exit_code, message}
        из container_status.state.terminated или last_state.terminated.
        Здесь остаются ВСЕ unhealthy-поды namespace-а: скоупинг по workload
        делают сами правила (oom.py / process_crash.py через same_workload),
        им нужен доступ и к пересозданным подам того же workload-а.
    pod_events — список k8s Events для target-пода, отсортированных
        по last_timestamp DESC. Каждый элемент: {type, reason, message, count}.
    core_dump_node — имя ноды, на которой найден core dump в /tmp/dump,
        или None если не найден / проверка не проводилась.
    """

    text: str
    container_terminated: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    pod_events: List[Dict[str, Any]] = field(default_factory=list)
    core_dump_node: Optional[str] = None


class K8sFacts:
    """Собирает верифицированные факты из кластера для Critic-агента."""

    @staticmethod
    async def collect(namespace: str) -> str:
        snap = await K8sFacts.collect_snapshot(namespace)
        return snap.text

    @staticmethod
    async def collect_snapshot(
        namespace: str, pod: Optional[str] = None
    ) -> K8sSnapshot:
        return await asyncio.get_event_loop().run_in_executor(
            None, K8sFacts._collect_sync, namespace, pod
        )

    @staticmethod
    def _collect_sync(namespace: str, pod: Optional[str] = None) -> K8sSnapshot:
        """namespace-снапшот; `pod` — target-workload для скоупинга текста.

        `pod` — имя пода (или workload-а) из label-ов алерта. Всё, что уходит
        в text blob из вывода ЧУЖИХ подов namespace-а, при известном target
        либо не инлайнится, либо перечисляется только именами: этот blob
        сканируют text-fallback-и правил, и «reason=OOMKilled» соседнего
        сервиса раньше давал Fact(oom_killed, observed=True, conf=0.95) на
        чужой инцидент. Если target неизвестен — скоупить не по чему, blob
        собирается как раньше (attribution-деградацию в этом случае делает
        сам OOMKilledRule).
        """
        try:
            k8s_config.load_incluster_config()
        except Exception:
            k8s_config.load_kube_config()

        results: List[str] = []
        container_terminated: Dict[str, Dict[str, Any]] = {}
        pod_events: List[Dict[str, Any]] = []

        try:
            v1 = client.CoreV1Api()

            # ── Unhealthy pods + restart counts ──────────────────────────
            pods = v1.list_namespaced_pod(namespace)
            unhealthy_pods = []
            for p in pods.items:
                if p.status.phase not in ("Running", "Succeeded"):
                    restarts = sum(
                        cs.restart_count for cs in (p.status.container_statuses or [])
                    )
                    unhealthy_pods.append((p.metadata.name, p.status.phase, restarts))

                    # Structured: container terminated state (last_state wins
                    # over current state — it has the actual exit reason)
                    for cs in p.status.container_statuses or []:
                        terminated = (
                            cs.last_state.terminated
                            if cs.last_state and cs.last_state.terminated
                            else cs.state.terminated if cs.state else None
                        )
                        if terminated:
                            container_terminated[p.metadata.name] = {
                                "reason": terminated.reason or "",
                                "exit_code": terminated.exit_code,
                                # terminated.message = хвост stderr контейнера,
                                # т.е. вывод приложения → редактируем до того,
                                # как он уйдёт в blob и в LLM-контекст.
                                "message": redact_pii(
                                    terminated.message or "",
                                    max_len=_TERMINATED_MSG_LEN,
                                ),
                                "container": cs.name,
                            }

            unhealthy_strs = [
                f"{name}: {phase} restarts={restarts}"
                for name, phase, restarts in unhealthy_pods
            ]
            results.append(
                f"Unhealthy pods in {namespace}: "
                f"{unhealthy_strs if unhealthy_strs else 'none'}"
            )

            # Добавляем terminated reasons в text blob тоже — OOMKilledRule
            # и другие regex-правила смогут их поймать. НО только по подам
            # target-workload-а: reason ЧУЖОГО сервиса в тексте становился
            # false anchor-ом (см. docstring _collect_sync). Чужие поды
            # перечисляем именами, без reason/exit_code — контекст «в ns есть
            # ещё падения» сохраняется, а regex-бейта в строке нет.
            foreign_terminated: List[str] = []
            for pod_name, info in container_terminated.items():
                if not info.get("reason"):
                    continue
                if pod and not same_workload(pod_name, pod):
                    foreign_terminated.append(pod_name)
                    continue
                results.append(
                    f"Container terminated: {pod_name}/{info['container']} "
                    f"reason={info['reason']} exit_code={info['exit_code']}"
                    + (f" — {info['message']}" if info.get("message") else "")
                )
            if foreign_terminated:
                results.append(
                    "Other pods in namespace with terminated containers "
                    f"(different workload than {pod}, reasons deliberately not "
                    "inlined — they are not evidence about this incident): "
                    + ", ".join(sorted(foreign_terminated))
                )

            # ── Peer namespace comparison ─────────────────────────────────
            peer = K8sFacts._peer_namespace(namespace)
            if peer is None and namespace.startswith("squad-"):
                logger.warning(
                    "k8s_facts_peer_namespace_unrecognized",
                    namespace=namespace,
                    hint="namespace starts with 'squad-' but doesn't match squad-<N>-<suffix> pattern",
                )
            if peer:
                try:
                    peer_pods = v1.list_namespaced_pod(peer)
                    peer_unhealthy = [
                        p.metadata.name
                        for p in peer_pods.items
                        if p.status.phase not in ("Running", "Succeeded")
                    ]
                    results.append(
                        f"Peer namespace {peer} unhealthy pods: "
                        f"{peer_unhealthy if peer_unhealthy else 'none (healthy)'}"
                    )
                except Exception as e:
                    logger.warning(
                        "k8s_facts_peer_unavailable", peer=peer, error=str(e)
                    )

            # ── Pod logs (tail 200 for first 3 unhealthy) ────────────────
            # previous=True первым: содержит вывод упавшего контейнера
            # (stacktrace, exit reason), а не свежего restarted-а.
            #
            # Скоуп: при известном target берём логи только его workload-а.
            # Логи соседнего сервиса — тот же false-anchor-материал, что и
            # чужие terminated-строки: "out of memory" из лога чужого пода
            # матчился text-fallback-ом OOMKilledRule.
            log_pods = [
                (name, phase, restarts)
                for name, phase, restarts in unhealthy_pods
                if not pod or same_workload(name, pod)
            ]
            skipped_log_pods = [
                name
                for name, _, _ in unhealthy_pods
                if pod and not same_workload(name, pod)
            ]
            for pod_name, _, _ in log_pods[:3]:
                fetched = False
                for prev in (True, False):
                    try:
                        logs = v1.read_namespaced_pod_log(
                            pod_name, namespace, tail_lines=200, previous=prev
                        )
                        if logs and logs.strip():
                            label = "previous" if prev else "current"
                            # redact_pii ДО попадания в blob: blob уезжает в
                            # LLM-промпт. max_len=None — усечения нет, тут
                            # нужен весь хвост логов (границу режет вызывающий).
                            safe_logs = redact_pii(logs.strip(), max_len=None)
                            results.append(
                                f"Logs {pod_name} ({label}, tail 200):\n{safe_logs}"
                            )
                            fetched = True
                            break
                    except Exception:
                        continue
                if not fetched:
                    logger.warning("k8s_facts_logs_unavailable", pod=pod_name)
            if skipped_log_pods:
                results.append(
                    "Other unhealthy pods in namespace (logs not collected — "
                    f"different workload than {pod}): "
                    + ", ".join(sorted(skipped_log_pods))
                )

            # ── Pod events ────────────────────────────────────────────────
            # Если указан конкретный pod — берём события только по нему,
            # иначе — последние Warning-события всего namespace (до 20).
            if pod:
                try:
                    ev_list = v1.list_namespaced_event(
                        namespace,
                        field_selector=f"involvedObject.name={pod}",
                    )
                    pod_events = _parse_events(ev_list.items)
                    if pod_events:
                        results.append(
                            f"Events for {pod}: "
                            + "; ".join(
                                f"{e['reason']}({e['count']}x): {e['message'][:80]}"
                                for e in pod_events[:10]
                            )
                        )
                except Exception as e:
                    logger.warning("k8s_facts_events_failed", pod=pod, error=str(e))
            else:
                try:
                    ev_list = v1.list_namespaced_event(
                        namespace,
                        field_selector="type=Warning",
                    )
                    warning_events = _parse_events(ev_list.items)[:20]
                    if warning_events:
                        results.append(
                            "Warning events in namespace: "
                            + "; ".join(
                                f"{e['reason']}({e['count']}x) on {e['object']}: {e['message'][:60]}"
                                for e in warning_events[:10]
                            )
                        )
                    pod_events = warning_events
                except Exception as e:
                    logger.warning("k8s_facts_ns_events_failed", error=str(e))

        except Exception as e:
            logger.error(
                "k8s_facts_collection_failed", namespace=namespace, error=str(e)
            )
            return K8sSnapshot(text=f"[k8s_facts unavailable: {e}]")

        # ── Core dump check ───────────────────────────────────────────────
        # Если у пода смонтирован host-dump (/tmp/dump), проверяем наличие
        # файлов через kubectl exec. Работает только если под Running.
        core_dump_node: Optional[str] = None
        if pod:
            try:
                all_pods = v1.list_namespaced_pod(namespace)
                for p in all_pods.items:
                    if pod in p.metadata.name and p.status.phase == "Running":
                        # Проверка mount_path в спеке пода, не создание tempfile.
                        has_dump_mount = any(
                            (vm.mount_path or "").startswith("/tmp/dump")  # nosec B108 — pod-spec inspection, not tempfile creation
                            for vm in (p.spec.containers[0].volume_mounts or [])
                            if p.spec.containers
                        )
                        if has_dump_mount:
                            node = p.spec.node_name
                            core_dump_node = node
                            results.append(
                                f"Core dump mount detected on node {node} at /tmp/dump "
                                f"(pod {p.metadata.name}) — crash dump may be available"
                            )
                        break
            except Exception as e:
                logger.warning("k8s_facts_coredump_check_failed", error=str(e))

        return K8sSnapshot(
            text="\n".join(results),
            container_terminated=container_terminated,
            pod_events=pod_events,
            core_dump_node=core_dump_node,
        )

    @staticmethod
    def _peer_namespace(namespace: str) -> str | None:
        """squad-1-kingdom2 → squad-2-kingdom2, squad-2-shared → squad-3-shared."""
        m = re.match(r"^(squad-)(\d+)(-.+)$", namespace)
        if m:
            n = int(m.group(2))
            # Сосед = N+1 (как обещает docstring). Если соседнего сквада нет,
            # это обрабатывается выше по стеку как "peer unavailable".
            peer_n = n + 1
            return f"{m.group(1)}{peer_n}{m.group(3)}"
        return None


def _parse_events(items: list) -> List[Dict[str, Any]]:
    """kubernetes Event list → нормализованные dicts, отсортированные по времени."""
    out = []
    for ev in items:
        last_ts = ev.last_timestamp or ev.event_time or ev.first_timestamp
        out.append(
            {
                "type": ev.type or "Unknown",
                "reason": ev.reason or "",
                "message": ev.message or "",
                "count": ev.count or 1,
                "object": (ev.involved_object.name if ev.involved_object else ""),
                "last_ts": str(last_ts) if last_ts else "",
            }
        )
    out.sort(key=lambda e: e["last_ts"], reverse=True)
    return out
