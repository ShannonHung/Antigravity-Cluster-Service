"""
tests/unit/test_pipeline_service.py

Unit tests for PipelineService — DeployServiceClient is fully mocked.
No network, no HTTP, no real deploy-service required.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.exceptions import UpstreamServiceException, ErrorCode, DeployServiceError
from app.domain.pipeline_models import (
    PipelineData,
    PipelineVariable,
    RunningPipelinesData,
)
from app.services.pipeline_service import PipelineService


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_pipeline(pipeline_id: int = 1, status: str = "running") -> PipelineData:
    return PipelineData(id=pipeline_id, status=status)


def _make_service(
    trigger_return: PipelineData | None = None,
    check_return: RunningPipelinesData | None = None,
    get_return: PipelineData | None = None,
    cancel_return: PipelineData | None = None,
    retry_return: PipelineData | None = None,
    side_effect: Exception | None = None,
) -> PipelineService:
    """Build a PipelineService backed by a fully mocked DeployServiceClient."""
    client = MagicMock()
    client.trigger_pipeline = AsyncMock(
        return_value=trigger_return or _make_pipeline(), side_effect=side_effect
    )
    client.check_running = AsyncMock(
        return_value=check_return or RunningPipelinesData(has_running=False, count=0, pipelines=[]),
        side_effect=side_effect,
    )
    client.get_pipeline = AsyncMock(
        return_value=get_return or _make_pipeline(), side_effect=side_effect
    )
    client.cancel_pipeline = AsyncMock(
        return_value=cancel_return or _make_pipeline(status="canceled"),
        side_effect=side_effect,
    )
    client.retry_pipeline = AsyncMock(
        return_value=retry_return or _make_pipeline(status="running"),
        side_effect=side_effect,
    )
    return PipelineService(client=client)


# ── trigger_pipeline ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trigger_pipeline_delegates_to_client():
    pipeline = _make_pipeline(1, "running")
    svc = _make_service(trigger_return=pipeline)
    result = await svc.trigger_pipeline(
        action="deploy", ref="main", extra_variables=[]
    )
    assert result.id == 1
    assert result.status == "running"


@pytest.mark.asyncio
async def test_trigger_pipeline_passes_variables():
    svc = _make_service()
    variables = [PipelineVariable(key="ENV", value="prod")]
    await svc.trigger_pipeline(action="deploy", ref="main", extra_variables=variables)
    svc._client.trigger_pipeline.assert_awaited_once_with(
        action="deploy", ref_name="main", variables=variables
    )


@pytest.mark.asyncio
async def test_trigger_pipeline_propagates_upstream_exception():
    exc = UpstreamServiceException("upstream error", detail={"error_code": "PIPELINE_CONFLICT"})
    svc = _make_service(side_effect=exc)
    with pytest.raises(UpstreamServiceException):
        await svc.trigger_pipeline(action="deploy", ref="main", extra_variables=[])


# ── check_running ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_running_returns_data():
    expected = RunningPipelinesData(
        has_running=True, count=1, pipelines=[_make_pipeline()]
    )
    svc = _make_service(check_return=expected)
    result = await svc.check_running(action="deploy", ref="main", extra_variables=[])
    assert result.has_running is True
    assert result.count == 1


# ── get_pipeline ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_pipeline_returns_data():
    svc = _make_service(get_return=_make_pipeline(42, "success"))
    result = await svc.get_pipeline(42)
    assert result.id == 42
    assert result.status == "success"


# ── cancel_pipeline ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_pipeline_returns_canceled_status():
    svc = _make_service(cancel_return=_make_pipeline(1, "canceled"))
    result = await svc.cancel_pipeline(1)
    assert result.status == "canceled"


# ── retry_pipeline ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_pipeline_returns_running_status():
    svc = _make_service(retry_return=_make_pipeline(1, "running"))
    result = await svc.retry_pipeline(1)
    assert result.status == "running"


# ── DeployServiceError mapping ────────────────────────────────────────────────

def test_deploy_service_error_maps_not_found():
    body = {"error": {"code": "NOT_FOUND", "message": "Pipeline 99 not found."}}
    err = DeployServiceError(http_status=404, body=body)
    resp = err.to_response()
    assert resp["error_code"] == ErrorCode.PIPELINE_NOT_FOUND
    assert resp["service"] == "deploy-service"
    assert "99" in resp["message"]


def test_deploy_service_error_maps_conflict():
    body = {"error": {"code": "CONFLICT", "message": "Already running.", "detail": {"pipeline_id": 5}}}
    err = DeployServiceError(http_status=409, body=body)
    resp = err.to_response()
    assert resp["error_code"] == ErrorCode.PIPELINE_CONFLICT
    assert resp["details"] == {"pipeline_id": 5}


def test_deploy_service_error_fallback_on_unknown_code():
    body = {"error": {"code": "UNKNOWN_CODE", "message": "Something went wrong."}}
    err = DeployServiceError(http_status=500, body=body)
    resp = err.to_response()
    assert resp["error_code"] == ErrorCode.UPSTREAM_ERROR


def test_deploy_service_error_fallback_on_empty_body():
    err = DeployServiceError(http_status=503, body={})
    resp = err.to_response()
    assert resp["error_code"] == ErrorCode.DEPLOY_SERVICE_UNAVAILABLE
    assert resp["service"] == "deploy-service"
    assert "503" in resp["message"]


def test_deploy_service_error_no_details_key_when_none():
    body = {"error": {"code": "NOT_FOUND", "message": "Not found."}}
    err = DeployServiceError(http_status=404, body=body)
    resp = err.to_response()
    assert "details" not in resp
