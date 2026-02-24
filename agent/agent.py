# agent/agent.py
from __future__ import annotations

from agent.planner import build_plan as planner_build_plan
from agent.executor import execute_plan as _execute_plan

async def build_plan(message: str, context_text: str | None = None, sn=None):
    plan, meta = await planner_build_plan(message, context_text, sn=sn)
    return plan, meta

async def execute_plan(sn, plan):
    return await _execute_plan(sn, plan)

