from __future__ import annotations
from typing import Any
def validate_grounding(report: dict[str,Any],evidence_ids: set[str],files: set[str])->dict[str,Any]:
    errors=[]
    def inspect(value: Any,key: str="")->None:
        if isinstance(value,dict):
            ids=value.get("evidence_ids",[])
            if ids is not None:
                if not isinstance(ids,list): errors.append("evidence_ids must be a list")
                else:
                    for item in ids:
                        if item not in evidence_ids: errors.append(f"unknown evidence id: {item}")
            cited=value.get("files",[])
            if cited is not None:
                if not isinstance(cited,list): errors.append("files must be a list")
                else:
                    for item in cited:
                        if item not in files: errors.append(f"unknown file: {item}")
            for child_key,child in value.items(): inspect(child,child_key)
        elif isinstance(value,list):
            for child in value: inspect(child,key)
    inspect(report)
    for recommendation in report.get("recommended_checks",[]) if isinstance(report.get("recommended_checks",[]),list) else []:
        text=recommendation.get("claim","") if isinstance(recommendation,dict) else str(recommendation)
        if "already " in text.lower() or "completed " in text.lower(): errors.append("recommendation claims completed action")
    return {"grounding_status":"passed" if not errors else "failed","errors":errors}
