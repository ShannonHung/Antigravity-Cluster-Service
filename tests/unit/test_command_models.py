"""Unit tests for command proxy domain models (mirrored from deploy-service)."""
from __future__ import annotations

from app.domain.command_models import (
    CommandStatus,
    HostType,
    CommandExecutionRequest,
    CommandExecutionResponse,
    CommandOutputFormat,
    OutputFormat,
    OutputJsonError,
    UserCommandWhitelist,
    CommandWhitelistConfig,
    PipelineStep,
    CommandTraceResponse,
    CommandLogLine,
)


def test_status_and_host_type_values():
    assert CommandStatus.RUNNING.value == "running"
    assert CommandStatus.SUCCESS.value == "success"
    assert HostType.IP.value == "ip"


def test_execution_request_defaults():
    req = CommandExecutionRequest(command_name="run_ansible", host="1.2.3.4", username="root")
    assert req.port == 22
    assert req.ssh_config == "default"
    assert req.host_type == HostType.IP
    assert req.option.timeout_seconds == 30
    assert req.arguments == {}


def test_execution_response_roundtrip():
    resp = CommandExecutionResponse(command_id="abc", status="running")
    dumped = resp.model_dump()
    assert dumped["command_id"] == "abc"
    assert dumped["status"] == "running"
    assert dumped["pgids"] == []
    # New JSON-output fields default to null so a caller that never passes
    # ?format=json sees an unchanged response.
    assert dumped["output_json"] is None
    assert dumped["output_json_error"] is None


def test_output_format_enum_values():
    assert OutputFormat.RAW.value == "raw"
    assert OutputFormat.JSON.value == "json"
    assert CommandOutputFormat.TEXT.value == "text"
    assert CommandOutputFormat.JSON.value == "json"


def test_execution_response_parses_output_json():
    # deploy-service populates these when ?format=json succeeds; cluster-service
    # forwards them verbatim. output_json is typed Any (object/array/scalar).
    resp = CommandExecutionResponse(
        command_id="abc",
        status="success",
        output='{"ok": true}',
        output_json={"ok": True},
    )
    assert resp.output_json == {"ok": True}
    assert resp.output_json_error is None
    # output keeps its raw string value alongside the parsed form.
    assert resp.output == '{"ok": true}'


def test_execution_response_parses_output_json_error():
    resp = CommandExecutionResponse(
        command_id="abc",
        status="failed",
        output_json_error="parse_failed",
    )
    assert resp.output_json is None
    assert resp.output_json_error is OutputJsonError.PARSE_FAILED


def test_whitelist_parses_nested_pipeline():
    wl = UserCommandWhitelist(
        name="cluster_proxy",
        allow_commands=[
            CommandWhitelistConfig(
                command_name="run_ansible",
                pipeline=[PipelineStep(command=["echo", "{x}"])],
            )
        ],
    )
    assert wl.allow_hosts == [".*"]
    assert wl.allow_commands[0].pipeline[0].command == ["echo", "{x}"]


def test_trace_response_minimal():
    tr = CommandTraceResponse(
        command_id="abc",
        status="running",
        next_byte_offset=10,
        next_line_num=2,
        lines=[CommandLogLine(num=1, content_html="hi")],
    )
    assert tr.total_size == 0
    assert tr.too_large is False
    assert tr.not_logged is False
    assert tr.lines[0].content_html == "hi"


def test_trace_response_parses_not_logged():
    # deploy-service now sets not_logged=true for commands run without logged:true.
    tr = CommandTraceResponse(
        command_id="abc",
        status="success",
        next_byte_offset=0,
        next_line_num=1,
        lines=[],
        not_logged=True,
    )
    assert tr.not_logged is True


def test_whitelist_config_script_version_fields():
    cfg = CommandWhitelistConfig(
        command_name="run_ansible",
        pipeline=[PipelineStep(command=["/opt/run.sh"])],
        checks_script_version=True,
        min_script_version="1.4.0",
    )
    assert cfg.checks_script_version is True
    assert cfg.min_script_version == "1.4.0"
    # Defaults stay off when deploy-service omits them.
    plain = CommandWhitelistConfig(
        command_name="noop", pipeline=[PipelineStep(command=["true"])]
    )
    assert plain.checks_script_version is False
    assert plain.min_script_version is None
    # output_format defaults to text when deploy-service omits it.
    assert plain.output_format is CommandOutputFormat.TEXT


def test_whitelist_config_surfaces_output_format():
    cfg = CommandWhitelistConfig(
        command_name="get_status",
        pipeline=[PipelineStep(command=["/opt/status.sh"])],
        output_format="json",
    )
    assert cfg.output_format is CommandOutputFormat.JSON


def test_execution_request_forwards_min_script_version():
    req = CommandExecutionRequest(
        command_name="run_ansible",
        host="1.2.3.4",
        username="root",
        min_script_version="2.0.0",
    )
    # Must survive JSON serialisation so the client forwards it to deploy-service.
    assert req.model_dump(mode="json")["min_script_version"] == "2.0.0"
