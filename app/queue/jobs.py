from redis import Redis
from rq import Queue
from app.config import settings

redis_conn = Redis.from_url(settings.REDIS_URL)
incident_queue = Queue('incidents', connection=redis_conn)
