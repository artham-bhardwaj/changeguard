from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from changeguard.demo_server import DemoHandler


def test_local_demo_serves_ui_and_real_pr_sample():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/") as response:
            page = response.read().decode("utf-8")
            assert response.headers["Content-Security-Policy"]
        assert "ChangeGuard" in page
        assert "Analyze Pull Request" in page

        with urlopen(f"{base_url}/api/demo") as response:
            sample = json.load(response)
        assert sample["demo_fixture"] is True
        assert sample["repository"] == "artham-bhardwaj/self-healing-platform"
        assert sample["analysis"]["api_impact"]["metrics"]["api_consumer_edges"] == 10
        api_impact = sample["analysis"]["api_impact"]
        assert api_impact["endpoints"][0]["endpoint_id"] in api_impact["changed_endpoint_ids"]
        assert api_impact["endpoints"][0]["route"] == "/self-healing/incidents/summary"
        assert sample["analysis"]["verification_result"]["status"] == "BLOCKED"
        assert "performed" in sample["analysis"]["investigation_report"]["uncertainties"][1]

        with urlopen(f"{base_url}/docs/demo.md") as response:
            assert b"Demo workflow" in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_local_demo_rejects_invalid_analysis_input_without_github_access():
    server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = Request(
        f"http://127.0.0.1:{server.server_port}/api/analyze",
        data=json.dumps({"repository": "../invalid/repo", "pull_number": 1}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        try:
            urlopen(request)
        except HTTPError as error:
            assert error.code == 400
            assert "OWNER/REPO" in json.load(error)["error"]
        else:
            raise AssertionError("Invalid repository input should be rejected.")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
