class IncidentGraph:
    def __init__(self):
        self.events = []
        self.dependencies = []

    def add_event(self, event):
        self.events.append(event)
        
    def correlate(self, topology: dict):
        # Time and Topology correlation
        for i, e1 in enumerate(self.events):
            for e2 in self.events[i+1:]:
                if abs((e1.timestamp - e2.timestamp).total_seconds()) < 60:
                    self.dependencies.append((e1, e2, "temporal"))
