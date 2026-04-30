from redis import Redis
from rq import Queue
from app.models.incident import NewRelicIncident, FullAnalysisReport
from app.agents.analyzer import AnalyzerAgent
from app.agents.hypothesis import HypothesisAgent
from app.agents.critic import CriticAgent
from app.agents.fix import FixAgent
from app.agents.risk import RiskAgent
from app.services.discord_service import discord_service
from app.services.k8s_service import k8s_service
from app.config import settings
import asyncio
import logging

redis_conn = Redis.from_url(settings.REDIS_URL)
q = Queue(connection=redis_conn)

async def process_incident_async(incident_data: dict):
    incident = NewRelicIncident(**incident_data)
    
    analyzer = AnalyzerAgent()
    hypo = HypothesisAgent()
    critic = CriticAgent()
    fixer = FixAgent()
    risker = RiskAgent()

    # 1. Analysis
    analysis = await analyzer.analyze(incident)
    
    # 2. Hypotheses
    hypotheses = await hypo.generate(analysis)
    
    # 3. Critic review
    final_cause = await critic.audit(analysis, hypotheses)
    
    # 4. Fix Suggestion
    fix_suggestion = await fixer.suggest(final_cause)
    
    # 5. Risk Assessment
    risk_report = await risker.assess(fix_suggestion)

    risk_level = "HIGH"
    if "LOW" in risk_report.upper(): risk_level = "LOW"
    elif "MEDIUM" in risk_report.upper(): risk_level = "MEDIUM"

    report = f"""
🚨 **SRE INCIDENT REPORT** 🚨
**Incident ID:** {incident.incident_id}
**Policy:** {incident.policy_name}
**Severity:** {incident.severity}

**Summary:**
{analysis[:500]}...

**Root Cause:**
{final_cause}

**Suggested Fix:**
{fix_suggestion}

**Risk Level:** {risk_level}
**Risk Analysis:** {risk_report}
    """
    
    await discord_service.send_report(report)
    logging.info(f"Processed incident {incident.incident_id}")

def start_worker_task(incident_data: dict):
    loop = asyncio.get_event_loop()
    loop.run_until_complete(process_incident_async(incident_data))

if __name__ == "__main__":
    from rq import Worker
    worker = Worker([q], connection=redis_conn)
    worker.work()
