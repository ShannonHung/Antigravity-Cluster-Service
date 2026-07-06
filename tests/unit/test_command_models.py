"""Unit tests for command proxy domain models (mirrored from deploy-service)."""
from __future__ import annotations

from app.domain.command_models import (
    CommandStatus,
    HostType,
    CommandExecutionRequest,
    CommandExecutionResponse,
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


def test_execution_request_forwards_min_script_version():
    req = CommandExecutionRequest(
        command_name="run_ansible",
        host="1.2.3.4",
        username="root",
        min_script_version="2.0.0",
    )
    # Must survive JSON serialisation so the client forwards it to deploy-service.
    assert req.model_dump(mode="json")["min_script_version"] == "2.0.0"
