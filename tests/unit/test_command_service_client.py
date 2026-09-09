"""Unit tests for CommandServiceClient — httpx transport is mocked, no network."""
from __future__ import annotations

import json
import httpx
import pytest
from unittest.mock import AsyncMock

from app.clients.command_service_client import CommandServiceClient
from app.core.exceptions import DeployServiceError
from app.domain.command_models import CommandExecutionRequest, OutputFormat


def _client_with_responses(*responses: httpx.Response) -> CommandServiceClient:
    tm = AsyncMock()
    tm.get_token = AsyncMock(return_value="tok")
    tm.refresh = AsyncMock(return_value=None)
    c = CommandServiceClient(base_url="http://deploy", token_manager=tm)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        r = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return r

    # Patch the AsyncClient used inside _request_with_retry to use a mock transport.
    transport = httpx.MockTransport(handler)
    c._transport = transport  # consumed by _request_with_retry (see impl)
    c._token_manager = tm
    return c


def _ok(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"data": data, "request_id": "x"})


async def test_get_all_commands_info_unwraps_data():
    c = _client_with_responses(_ok({
        "name": "cluster_proxy",
        "allow_hosts": [".*"],
        "deny_hosts": [],
        "allow_commands": [],
    }))
    wl = await c.get_all_commands_info()
    assert wl.name == "cluster_proxy"


async def test_execute_command_posts_and_unwraps():
    c = _client_with_responses(_ok({"command_id": "abc", "status": "running"}))
    resp = await c.execute_command(
        CommandExecutionRequest(command_name="run_ansible", host="1.2.3.4", username="root")
    )
    assert resp.command_id == "abc"
    assert resp.status == "running"


async def test_non_2xx_raises_deploy_service_error():
    c = _client_with_responses(
        httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "nope"}})
    )
    with pytest.raises(DeployServiceError):
        await c.get_command_result("missing")


async def test_get_command_result_defaults_to_raw_format():
    captured: dict = {}
    tm = AsyncMock()
    tm.get_token = AsyncMock(return_value="tok")
    c = CommandServiceClient(base_url="http://deploy", token_manager=tm)

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={"data": {"command_id": "abc", "status": "success"}, "request_id": "x"})

    c._transport = httpx.MockTransport(handler)
    await c.get_command_result("abc")
    assert captured["url"].params.get("format") == "raw"


async def test_get_command_result_forwards_json_format_and_unwraps():
    captured: dict = {}
    tm = AsyncMock()
    tm.get_token = AsyncMock(return_value="tok")
    c = CommandServiceClient(base_url="http://deploy", token_manager=tm)

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200,
            json={
                "data": {
                    "command_id": "abc",
                    "status": "success",
                    "output": '{"ok": true}',
                    "output_json": {"ok": True},
                    "output_json_error": None,
                },
                "request_id": "x",
            },
        )

    c._transport = httpx.MockTransport(handler)
    resp = await c.get_command_result("abc", OutputFormat.JSON)
    assert captured["url"].params.get("format") == "json"
    assert resp.output_json == {"ok": True}
    assert resp.output == '{"ok": true}'
    assert resp.output_json_error is None


async def test_401_triggers_refresh_and_retry():
    c = _client_with_responses(
        httpx.Response(401, json={"error": {"code": "AUTH_ERROR", "message": "expired"}}),
        _ok({"command_id": "abc", "status": "running"}),
    )
    resp = await c.get_command_result("abc")
    assert resp.command_id == "abc"
    c._token_manager.refresh.assert_awaited_once()
