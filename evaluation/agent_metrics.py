from __future__ import annotations
from typing import Any
def agent_metrics(runs: list[dict[str,Any]])->dict[str,float]:
    total=max(1,len(runs)); calls=[len(run.get("tool_calls",[])) for run in runs]
    return {"tool_call_success_rate":sum(bool(run.get("tool_success",False)) for run in runs)/total,"grounded_claim_rate":sum(run.get("grounding_status")=="passed" for run in runs)/total,"unnecessary_tool_calls":sum(run.get("unnecessary_tool_calls",0) for run in runs)/total,"average_tool_calls":sum(calls)/total,"fallback_rate":sum(run.get("mode")=="deterministic_fallback" for run in runs)/total}
