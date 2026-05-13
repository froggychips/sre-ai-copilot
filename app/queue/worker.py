import logging

from rq import Connection, Worker

from app.queue.jobs import incident_queue, redis_conn


def process_incident(incident_data):
    """
    Worker-side task execution with retry and timeout.
    """
    logging.info(f"Processing incident: {incident_data.get('incident_id')}")
    # analyzer = AnalyzerAgent()  # TODO: Use this
    # Реализация пайплайна (логика из Phase 2)
    # ...
    logging.info("Task completed")


if __name__ == "__main__":
    with Connection(redis_conn):
        worker = Worker([incident_queue])
        worker.work()
