import time
from app.queue.jobs import incident_queue
from app.agents.analyzer import AnalyzerAgent
from rq import Worker, Connection
from app.queue.jobs import redis_conn
import logging

def process_incident(incident_data):
    """
    Worker-side task execution with retry and timeout.
    """
    logging.info(f"Processing incident: {incident_data.get('incident_id')}")
    analyzer = AnalyzerAgent()
    # Реализация пайплайна (логика из Phase 2)
    # ...
    logging.info("Task completed")

if __name__ == '__main__':
    with Connection(redis_conn):
        worker = Worker([incident_queue])
        worker.work()
