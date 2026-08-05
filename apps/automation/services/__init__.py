"""Domain logic for the automation service."""

from services.clients import AiOutput, PipelineClients
from services.events import AutomationEventHandler, register_consumers
from services.imports import ImportOutcome, ImportService, RowResult, slugify
from services.jobs import JobService
from services.pipeline import PipelineRunner, RunOutcome
from services.stages import StageContext, StageSkipped

__all__ = [
    "AiOutput",
    "AutomationEventHandler",
    "ImportOutcome",
    "ImportService",
    "JobService",
    "PipelineClients",
    "PipelineRunner",
    "RowResult",
    "RunOutcome",
    "StageContext",
    "StageSkipped",
    "register_consumers",
    "slugify",
]
