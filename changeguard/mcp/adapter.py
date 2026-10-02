import asyncio
import sys
from typing import Any
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from changeguard.evidence.models import EvidenceBundle, RetrievedEvidence
from changeguard.models.schemas import CommitMetadata, DiffResult, JavaSourceSnapshot, RepoStatus
class MCPToolError(RuntimeError): pass
class MCPToolPort:
    transport="mcp"
    def __init__(
        self,
        server_command: list[str]|None=None,
        evidence_database: str="changeguard.sqlite3",
        *,
        server_environment: dict[str,str]|None=None,
    )->None:
        self.server_command=server_command or [sys.executable,"-m","changeguard.mcp.server","--database",evidence_database]
        self.server_environment=server_environment
    def call_tool(self,name: str,arguments: dict[str,Any])->dict[str,Any]: return asyncio.run(self._call_async(name,arguments))
    async def _call_async(self,name: str,arguments: dict[str,Any])->dict[str,Any]:
        async with stdio_client(StdioServerParameters(command=self.server_command[0],args=self.server_command[1:],env=self.server_environment)) as (reader,writer):
            async with ClientSession(reader,writer) as session: await session.initialize(); result=await session.call_tool(name,arguments)
        if result.is_error or not result.structured_content: raise MCPToolError(f"MCP tool {name} failed: {result.content}")
        return dict(result.structured_content)
    def repo_status(self,repo_path: str)->RepoStatus: return RepoStatus(**self.call_tool("get_repo_status",{"repo_path":repo_path}))
    def git_diff(self,repo_path: str,base: str,head: str)->DiffResult: return DiffResult(**self.call_tool("get_git_diff",{"repo_path":repo_path,"base":base,"head":head}))
    def recent_commits(self,repo_path: str,limit: int,revision: str|None=None)->list[CommitMetadata]:
        arguments: dict[str,Any]={"repo_path":repo_path,"limit":limit}
        if revision is not None: arguments["revision"]=revision
        return [CommitMetadata(**x) for x in self.call_tool("get_recent_commits",arguments)["commits"]]
    def java_sources(self,repo_path: str,revision: str)->JavaSourceSnapshot:
        value=self.call_tool("get_java_sources",{"repo_path":repo_path,"revision":revision})
        return JavaSourceSnapshot(**value)
    def revision_files(self,repo_path: str,revision: str,max_files: int=10_000)->tuple[str,...]:
        return tuple(self.call_tool("get_revision_files",{"repo_path":repo_path,"revision":revision,"max_files":max_files})["files"])
    def revision_file(self,repo_path: str,revision: str,path: str,max_bytes: int=512*1024)->str:
        return str(self.call_tool("get_revision_file",{"repo_path":repo_path,"revision":revision,"path":path,"max_bytes":max_bytes})["content"])
    def get_file(self,repo_path: str,path: str)->str: return str(self.call_tool("get_file",{"repo_path":repo_path,"path":path})["content"])
    def search_evidence(self,query: str,top_k: int=5,source_type: str|None=None,repository: str|None=None)->EvidenceBundle:
        value=self.call_tool("search_evidence",{"query":query,"top_k":top_k,"source_type":source_type,"repository":repository}); value["results"]=[RetrievedEvidence(**x) for x in value["results"]]; return EvidenceBundle(**value)
