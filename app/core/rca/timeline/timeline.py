class TimelineEngine:
    @staticmethod
    def build(graph) -> list:
        # Сортировка событий по времени для RCA Timeline
        sorted_events = sorted(graph.nodes, key=lambda x: x.timestamp)
        timeline = []
        for e in sorted_events:
            timeline.append({
                "time": str(e.timestamp),
                "event": e.event_type,
                "source": e.source
            })
        return timeline
