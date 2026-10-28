from __future__ import annotations

from changeguard.analysis import ChangeAnalysisService
from changeguard.code_intelligence.endpoints import standard_route
from changeguard.code_intelligence.java import ChangedLineRange
from changeguard.code_intelligence.python_lang import PythonSymbolMapper, extract_python_endpoints
from changeguard.code_intelligence.script_lang import ScriptSymbolMapper
from changeguard.storage import SQLiteAnalysisStore
from tests.conftest import git


def names(ids):
    return sorted(item.split("::", 1)[1] for item in ids)


def edges(analysis):
    return {(e.caller_id.split("::")[1], e.method_name, e.resolution) for e in analysis.call_edges}


def test_python_calls_resolve_through_imports_and_constructors():
    sources = {
        "app/db.py": "def query(sql):\n    return []\n\nclass Repo:\n    def find(self, id):\n        return self._load(id)\n    def _load(self, id):\n        return query('x')\n",
        "app/services/users.py": (
            "from ..db import Repo\nfrom app import db as database\nimport os\n\n"
            "class UserService:\n    def __init__(self):\n        self.repo = Repo()\n"
            "    def get(self, id):\n        os.path.join('a')\n        database.query('y')\n        return self.repo.find(id)\n"
        ),
        "app/api.py": "from .services.users import UserService\n\ndef read_user(id):\n    service = UserService()\n    return service.get(id)\n",
    }
    analysis = PythonSymbolMapper().analyze(
        sources, [ChangedLineRange("app/db.py", 8, 1, 8, 1)], ["app/db.py"]
    )
    found = edges(analysis)
    assert ("app.db.Repo.find()", "_load", "resolved") in found
    assert ("app.services.users.UserService.get()", "find", "resolved") in found
    assert ("app.services.users.UserService.get()", "query", "resolved") in found
    assert ("app.api.read_user()", "get", "resolved") in found
    assert not any(name == "join" for _, name, _ in found)  # stdlib calls are ignored
    assert names(analysis.changed_symbol_ids) == ["app.db.Repo._load()"]
    assert names(analysis.direct_dependents) == ["app.db.Repo.find()"]
    assert names(analysis.indirect_dependents) == ["app.api.read_user()", "app.services.users.UserService.get()"]


def test_fastapi_and_flask_endpoints_with_prefixes():
    sources = {
        "backend/routers/users.py": (
            "from fastapi import APIRouter\nrouter = APIRouter(prefix='/users')\n\n"
            "@router.get('/{user_id:int}')\nasync def read_user(user_id: int):\n    return {}\n"
        ),
        "backend/main.py": (
            "from fastapi import FastAPI\nfrom .routers import users\napp = FastAPI()\n"
            "app.include_router(users.router, prefix='/api')\n\n@app.get('/health')\ndef health():\n    return 'ok'\n"
        ),
        "web/views.py": (
            "from flask import Blueprint, Flask\nbp = Blueprint('items', __name__, url_prefix='/items')\napp = Flask(__name__)\n\n"
            "@bp.route('/<int:item_id>', methods=['GET', 'DELETE'])\ndef item(item_id):\n    return ''\n\n"
            "app.register_blueprint(bp, url_prefix='/api/items')\n"
        ),
    }
    mapper = PythonSymbolMapper()
    found = {(e.http_method, e.route, e.framework) for e in extract_python_endpoints(mapper.parse_project(sources), "rev")}
    assert found == {
        ("GET", "/health", "fastapi"),
        ("GET", "/api/users/{user_id}", "fastapi"),
        ("GET", "/api/items/{item_id}", "flask"),
        ("DELETE", "/api/items/{item_id}", "flask"),
    }


def test_typescript_components_imports_and_classes():
    sources = {
        "src/lib/api.ts": "export async function fetchUser(id: string) {\n  return fetch(`/api/users/${id}`)\n}\nexport interface User { id: string }\n",
        "src/components/UserCard.tsx": "import { fetchUser } from '@/lib/api'\nexport default function UserCard({ id }: { id: string }) {\n  fetchUser(id)\n  return <div />\n}\n",
        "src/app/users/page.tsx": "import UserCard from '../../components/UserCard'\nexport default function Page() {\n  return <UserCard id=\"1\" />\n}\n",
        "src/store.ts": "export class Store {\n  load() { return 1 }\n}\n",
        "src/use.ts": "import { Store } from './store'\nconst store = new Store()\nexport const useStore = () => store.load()\nexport const settings = { theme: 'dark' }\n",
    }
    analysis = ScriptSymbolMapper().analyze(
        sources, [ChangedLineRange("src/lib/api.ts", 2, 1, 2, 1)], ["src/lib/api.ts"]
    )
    found = edges(analysis)
    assert ("src/components/UserCard.UserCard()", "fetchUser", "resolved") in found
    assert ("src/app/users/page.Page()", "UserCard", "resolved") in found
    assert ("src/use.useStore()", "load", "resolved") in found
    assert names(analysis.direct_dependents) == ["src/components/UserCard.UserCard()"]
    assert names(analysis.indirect_dependents) == ["src/app/users/page.Page()"]
    kinds = {s.qualified_name: s.kind for s in analysis.symbols}
    assert kinds["src/lib/api.User"] == "type" and kinds["src/use.settings"] == "constant"


def test_express_and_next_endpoints():
    sources = {
        "server/routes/users.js": (
            "const express = require('express')\nconst router = express.Router()\n"
            "router.get('/:id', (req, res) => {\n  res.json({})\n})\nrouter.post('/', createUser)\n"
            "function createUser(req, res) { res.json({}) }\nmodule.exports = router\n"
        ),
        "server/app.js": "const express = require('express')\nconst users = require('./routes/users')\nconst app = express()\napp.use('/api/users', users)\n",
        "web/app/api/health/route.ts": "export async function GET() {\n  return Response.json({})\n}\n",
        "web/pages/api/legacy/index.ts": "export default function handler(req: any, res: any) {\n  res.end()\n}\n",
    }
    mapper = ScriptSymbolMapper()
    found = {(e.http_method, e.route, e.framework, e.declaring_method)
             for e in mapper.extract_endpoints(mapper.parse_project(sources), "rev")}
    assert found == {
        ("GET", "/api/users/{id}", "express", "server/routes/users.<GET /:id>()"),
        ("POST", "/api/users", "express", "server/routes/users.createUser()"),
        ("GET", "/api/health", "nextjs", "web/app/api/health/route.GET()"),
        ("ANY", "/api/legacy", "nextjs", "web/pages/api/legacy/index.handler()"),
    }


def test_standard_route_parameters():
    assert standard_route("/a/<int:x>/:y/[z]/[...rest]/{w:int}") == "/a/{x}/{y}/{z}/{rest}/{w}"


def test_full_stack_change_flows_from_fastapi_to_react(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    files = {
        "api/main.py": "from fastapi import FastAPI\napp = FastAPI()\n\n@app.get('/api/orders/{order_id}')\ndef get_order(order_id: int):\n    return {'id': order_id}\n",
        "web/src/api.ts": "export function loadOrder(id: string) {\n  return fetch(`/api/orders/${id}`)\n}\n",
        "web/src/OrderView.tsx": "import { loadOrder } from './api'\nexport function OrderView() {\n  loadOrder('1')\n  return <div />\n}\n",
    }
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "api/main.py").write_text(files["api/main.py"].replace("{'id': order_id}", "{'id': order_id, 'v': 2}"), encoding="utf-8")
    git(repo, "commit", "-am", "change order payload")
    head = git(repo, "rev-parse", "HEAD")

    result = ChangeAnalysisService(SQLiteAnalysisStore(str(tmp_path / "db.sqlite3"))).analyze(
        repo_path=str(repo), base=base, head=head
    ).to_dict()

    assert "1 Python file(s)" in result["change_summary"]
    assert [s["qualified_name"] for s in result["changed_symbols"]] == ["api.main.get_order()"]
    api = result["api_impact"]
    assert [e["endpoint_identity"] for e in api["edges"]] == ["GET /api/orders/{order_id}"]
    assert len(api["impacted_consumer_ids"]) == 1
    indirect = names(result["code_analysis"]["indirect_dependents"])
    assert indirect == ["web/src/OrderView.OrderView()", "web/src/api.loadOrder()"]
