from prometheus_client import Counter, Summary

# Метрики для отслеживания качества ИИ
RESOLUTION_ACCURACY = Counter("ai_resolution_accuracy_total", "Count of accepted/rejected fixes", ["result"])
HALLUCINATION_RATE = Counter("ai_hallucinations_total", "Count of detected AI hallucinations")
RESOLUTION_TIME = Summary("ai_resolution_time_seconds", "Time taken for incident resolution")
FEEDBACK_SCORE = Summary("ai_feedback_score", "User feedback score for AI fixes")

def track_result(is_accepted: bool):
    RESOLUTION_ACCURACY.labels(result="accepted" if is_accepted else "rejected").inc()

def track_hallucination():
    HALLUCINATION_RATE.inc()

def observe_resolution_time(seconds: float):
    RESOLUTION_TIME.observe(seconds)

def track_feedback(score: int):
    FEEDBACK_SCORE.observe(score)
