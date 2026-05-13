from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog
from kubernetes import client
from kubernetes import config as k8s_config

logger = structlog.get_logger()


@dataclass
class K8sSnapshot:
    """Structured k8s data для diagnostic rules.

    text — человекочитаемый blob (идёт в logs_summary / LLM context).
    container_terminated — pod_name → {reason, exit_code, message}
        из container_status.state.terminated или last_state.terminated.
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
                                "message": (terminated.message or "")[:200],
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
            # и другие regex-правила смогут их поймать.
            for pod_name, info in container_terminated.items():
                if info.get("reason"):
                    results.append(
                        f"Container terminated: {pod_name}/{info['container']} "
                        f"reason={info['reason']} exit_code={info['exit_code']}"
                        + (f" — {info['message']}" if info.get("message") else "")
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
            for pod_name, _, _ in unhealthy_pods[:3]:
                fetched = False
                for prev in (True, False):
                    try:
                        logs = v1.read_namespaced_pod_log(
                            pod_name, namespace, tail_lines=200, previous=prev
                        )
                        if logs and logs.strip():
                            label = "previous" if prev else "current"
                            results.append(
                                f"Logs {pod_name} ({label}, tail 200):\n{logs.strip()}"
                            )
                            fetched = True
                            break
                    except Exception:
                        continue
                if not fetched:
                    logger.warning("k8s_facts_logs_unavailable", pod=pod_name)

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
            peer_n = 2 if n != 2 else 3
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
