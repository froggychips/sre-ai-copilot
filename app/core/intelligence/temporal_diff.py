from typing import List, Dict, Any

class TemporalDiffEngine:
    @staticmethod
    def compare(before_events: List[Dict], after_events: List[Dict]) -> Dict[str, Any]:
        """
        Сравнивает состояние системы до и после инцидента.
        """
        before_types = {e.get("event_type") for e in before_events}
        after_types = {e.get("event_type") for e in after_events}
        
        new_events = list(after_types - before_types)
        disappeared_events = list(before_types - after_types)
        
        # Поиск аномальных всплесков (упрощенно)
        spikes = []
        for etype in after_types & before_types:
            before_count = len([e for e in before_events if e.get("event_type") == etype])
            after_count = len([e for e in after_events if e.get("event_type") == etype])
            if after_count > before_count * 2:
                spikes.append(etype)

        return {
            "new_events": new_events,
            "disappeared_events": disappeared_events,
            "spike_events": spikes
        }
