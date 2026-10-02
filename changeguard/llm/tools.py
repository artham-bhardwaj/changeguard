from __future__ import annotations
TOOLS=[
 {"type":"function","function":{"name":"get_repo_status","description":"Read current branch, HEAD, and modified files.","parameters":{"type":"object","properties":{"repo_path":{"type":"string"}},"required":["repo_path"]}}},
 {"type":"function","function":{"name":"get_git_diff","description":"Read a bounded Git diff between two commits.","parameters":{"type":"object","properties":{"repo_path":{"type":"string"},"base":{"type":"string"},"head":{"type":"string"}},"required":["repo_path","base","head"]}}},
 {"type":"function","function":{"name":"get_file","description":"Read a bounded text file inside a repository.","parameters":{"type":"object","properties":{"repo_path":{"type":"string"},"path":{"type":"string"}},"required":["repo_path","path"]}}},
 {"type":"function","function":{"name":"get_recent_commits","description":"Read recent commit metadata.","parameters":{"type":"object","properties":{"repo_path":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":100}},"required":["repo_path"]}}},
 {"type":"function","function":{"name":"search_evidence","description":"Search provenance-bearing historical engineering evidence.","parameters":{"type":"object","properties":{"query":{"type":"string"},"top_k":{"type":"integer","minimum":1,"maximum":10},"source_type":{"type":"string"},"repository":{"type":"string"}},"required":["query"]}}}
]
TOOL_NAMES={tool["function"]["name"] for tool in TOOLS}
