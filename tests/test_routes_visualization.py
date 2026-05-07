#!/usr/bin/env python3
"""Integration tests for the visualization blueprint."""

import app as app_module


def test_index_renders():
    client = app_module.app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"html" in response.data.lower()


def test_visualize_not_found():
    client = app_module.app.test_client()
    response = client.get("/api/visualize/ZZZZNOTREAL999")
    assert response.status_code == 404


def test_results_not_found_404():
    """GET /api/results/NONEXISTENT returns 404."""
    client = app_module.app.test_client()
    response = client.get("/api/results/NONEXISTENT")
    assert response.status_code == 404


def test_visualize_endpoint_with_mocked_data(monkeypatch):
    """Monkeypatch build_full_json_from_db to return data, verify 200."""
    mock_result = {
        "snapshot_json": {
            "main": "TESTPROT",
            "proteins": [],
            "interactions": [],
            "interactors": [],
        },
        "ctx_json": {},
    }
    monkeypatch.setattr(
        "routes.visualization.build_full_json_from_db",
        lambda protein: mock_result,
    )
    # Also mock the visualizer import
    monkeypatch.setattr(
        "builtins.__import__",
        lambda *a, **kw: __builtins__.__import__(*a, **kw)
        if a[0] != "visualizer"
        else type("mod", (), {"create_visualization_from_dict": lambda d: "<html>OK</html>"})(),
    ) if False else None  # Skip complex import mock

    # Simpler approach: mock at the visualization route level
    import routes.visualization as viz_mod

    def fake_build(protein):
        return mock_result

    monkeypatch.setattr(viz_mod, "build_full_json_from_db", fake_build)

    # We need to also handle the visualizer import inside the route
    import sys
    import types as stdlib_types
    fake_visualizer = stdlib_types.ModuleType("visualizer")
    fake_visualizer.create_visualization_from_dict = lambda d: "<html>mocked</html>"
    monkeypatch.setitem(sys.modules, "visualizer", fake_visualizer)

    client = app_module.app.test_client()
    response = client.get("/api/visualize/TESTPROT")
    assert response.status_code == 200
    assert b"mocked" in response.data


def test_visualize_spa_opt_in_with_mocked_data(monkeypatch):
    """React SPA is still reachable explicitly, but not the default shell."""
    mock_result = {
        "snapshot_json": {
            "main": "TESTPROT",
            "proteins": [],
            "interactions": [],
            "interactors": [],
        },
        "ctx_json": {},
    }

    import routes.visualization as viz_mod

    monkeypatch.setattr(viz_mod, "build_full_json_from_db", lambda protein: mock_result)

    client = app_module.app.test_client()
    response = client.get("/api/visualize/TESTPROT?spa=1")
    assert response.status_code == 200
    assert response.headers["X-Viz-Shell"] == "spa"
    assert b"/static/react/app.js" in response.data
