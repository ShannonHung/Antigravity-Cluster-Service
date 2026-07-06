"""Maps deploy-service command errors onto cluster-service ErrorCode."""
from __future__ import annotations

from app.core.exceptions import DeployServiceError, ErrorCode


def test_command_execution_error_maps():
    body = {"error": {"code": "COMMAND_EXECUTION_ERROR", "message": "ansible failed"}}
    err = DeployServiceError(http_status=500, body=body)
    resp = err.to_response()
    assert resp["error_code"] == ErrorCode.COMMAND_EXECUTION_FAILED


def test_command_not_found_still_maps_via_status():
    body = {"error": {"code": "NOT_FOUND", "message": "Command abc not found."}}
    err = DeployServiceError(http_status=404, body=body)
    resp = err.to_response()
    # Existing NOT_FOUND mapping is reused; just assert it resolves to a code.
    assert resp["error_code"] is not None


def test_script_version_mismatch_maps_via_code():
    body = {
        "error": {
            "code": "SCRIPT_VERSION_MISMATCH",
            "message": "run.sh on the target is version 1.0.0, below the required 1.4.0.",
            "detail": {"script": "run.sh", "actual": "1.0.0", "required": "1.4.0"},
        }
    }
    err = DeployServiceError(http_status=412, body=body)
    resp = err.to_response()
    assert resp["error_code"] == ErrorCode.SCRIPT_VERSION_MISMATCH
    # deploy-service's structured detail is forwarded verbatim to the caller.
    assert resp["details"]["required"] == "1.4.0"


def test_script_version_mismatch_maps_via_status_fallback():
    # Even with an unrecognised body code, HTTP 412 resolves to the version code.
    err = DeployServiceError(http_status=412, body={})
    resp = err.to_response()
    assert resp["error_code"] == ErrorCode.SCRIPT_VERSION_MISMATCH
