"""MCP server smoke tests: drive the JSON-RPC handler directly."""

import json

from meetily_connector import mcp_server


def _call(cfg, method, params=None, request_id=1):
    request = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        request["params"] = params
    return mcp_server._handle(cfg, request)


def _tool(cfg, name, arguments=None):
    response = _call(cfg, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response["result"]["isError"] is False, response
    return response["result"]["content"][0]["text"]


def test_initialize_and_tools_list(cfg):
    init = _call(cfg, "initialize")
    assert init["result"]["serverInfo"]["name"] == "meetily"
    assert _call(cfg, "notifications/initialized", request_id=None) is None
    tools = _call(cfg, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "list_meetings", "get_meeting", "get_transcript",
        "search_meetings", "get_summary", "connector_status",
    }


def test_list_and_get(cfg):
    listing = _tool(cfg, "list_meetings")
    assert "meeting-done" in listing and "summary=completed" in listing
    detail = _tool(cfg, "get_meeting", {"meeting_id": "meeting-done"})
    assert "duration_seconds: 180" in detail


def test_transcript_paging(cfg):
    page1 = _tool(cfg, "get_transcript",
                  {"meeting_id": "meeting-huge", "max_chars": 1000})
    assert "TRUNCATED at char 1000" in page1
    page2 = _tool(cfg, "get_transcript",
                  {"meeting_id": "meeting-huge", "max_chars": 1000, "offset": 1000})
    assert page2.split("\n[TRUNCATED")[0] != page1.split("\n[TRUNCATED")[0]


def test_search_and_summary(cfg):
    hits = _tool(cfg, "search_meetings", {"query": "deployment"})
    assert "meeting-done" in hits
    summary = _tool(cfg, "get_summary", {"meeting_id": "meeting-done"})
    assert json.loads(summary)["summary"] == "Sprint planning recap"
    pending = _tool(cfg, "get_summary", {"meeting_id": "meeting-pending"})
    assert "PENDING" in pending


def test_unknown_tool_is_error_result_not_crash(cfg):
    response = _call(cfg, "tools/call", {"name": "drop_tables", "arguments": {}})
    assert response["result"]["isError"] is True


def test_status_reports_counts(cfg):
    status = _tool(cfg, "connector_status")
    assert "meetings: 7" in status
