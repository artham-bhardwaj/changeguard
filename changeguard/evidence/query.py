from __future__ import annotations
import re
from changeguard.models.schemas import AnalysisState
STOP={"py","java","txt","md","src","main","test","tests","change","diff","return","class","public","private","from","import"}
def build_change_query(state: AnalysisState, diff_text: str="") -> str:
    parts=[state.repository.rsplit("/",1)[-1]]
    for path in state.changed_files:
        parts.extend(re.split(r"[^a-zA-Z0-9_]+",path))
    parts.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}",diff_text)[:30])
    selected=[]; seen=set()
    for item in parts:
        word=item.lower()
        if word not in STOP and word not in seen: selected.append(word); seen.add(word)
        if len(selected)==12: break
    return " ".join(selected) or "repository change"
