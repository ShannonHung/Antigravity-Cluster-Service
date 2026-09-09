"""
app/domain/command_models.py

Pydantic models for the command-execution proxy. Mirrored from deploy-service's
app/domain/command.py but trimmed to the request/response/whitelist shapes
cluster-service needs — no runtime dataclasses, no asyncssh. cluster-service
only forwards JSON to deploy-service; it never executes SSH itself.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class CommandStatus(str, Enum):
    RUNNING = "running"
    KILLING = "killing"
    KILLED = "killed"
    SUCCESS = "success"
    FAILED = "failed"


class HostType(str, Enum):
    IP = "ip"
    BASTION = "bastion"
    HOSTNAME = "hostname"
    CLUSTER = "cluster"


class OutputFormat(str, Enum):
    """How the caller wants a command's ``output`` returned on the poll endpoint.

    Mirrors deploy-service's ``OutputFormat``. ``raw`` (the default) is the
    historical response — ``output`` stays a string and the JSON fields stay
    null. ``json`` asks deploy-service to additionally parse ``output`` into
    ``output_json`` (permitted only for commands whose whitelist entry declares
    ``output_format: "json"``). cluster-service only forwards this as the
    ``?format=`` query param; the parsing itself happens upstream.
    """

    RAW = "raw"
    JSON = "json"


class CommandOutputFormat(str, Enum):
    """The operator's declaration of what a command emits on stdout.

    Part of the command's contract, not the caller's preference: declaring
    ``json`` PERMITS ``?format=json``, it never triggers parsing on its own.
    Surfaced in the ``/command/.../info`` responses cluster-service proxies so
    callers can discover which commands accept ``?format=json``.
    """

    TEXT = "text"
    JSON = "json"


class OutputJsonError(str, Enum):
    """Why ``output_json`` is null even though ``?format=json`` was requested.

    Mirrored verbatim from deploy-service so the proxied value round-trips as a
    typed enum rather than a bare string:

    - ``parse_failed`` — the command declared ``output_format: "json"`` but its
      stdout was not valid JSON (the remote script broke its own contract).
    - ``output_unavailable`` — the command succeeded, but its stdout was lost
      during cross-pod orphan-run recovery.
    - ``not_applicable`` — the command is not in a success state, so there was
      never stdout to parse.
    """

    PARSE_FAILED = "parse_failed"
    OUTPUT_UNAVAILABLE = "output_unavailable"
    NOT_APPLICABLE = "not_applicable"


# ── Whitelist configuration ──────────────────────────────────────────────────

class CommandArgumentConfig(BaseModel):
    name: str
    type: str
    validation_regex: str = ""
    required: bool = True
    description: str = ""


class PipelineStep(BaseModel):
    command: List[str]


class CommandWhitelistConfig(BaseModel):
    command_name: str
    description: str = ""
    disconnects_ssh: bool = False
    killable: bool = False
    logged: bool = False
    # Script-version pre-check (mirrored from deploy-service). cluster-service
    # only surfaces these in the /command/.../info responses it proxies; the
    # actual version check runs on deploy-service, so no validator is mirrored.
    checks_script_version: bool = False
    min_script_version: Optional[str] = None
    # Operator's declaration of the command's stdout contract. "json" is what
    # PERMITS a caller to ask for ?format=json on the poll endpoint; cluster-service
    # only surfaces it here so callers can discover which commands accept it (the
    # permission check and parsing both run upstream on deploy-service).
    output_format: CommandOutputFormat = CommandOutputFormat.TEXT
    pipeline: List[PipelineStep]
    arguments: List[CommandArgumentConfig] = []


class UserCommandWhitelist(BaseModel):
    name: str = "cluster_proxy"
    allow_hosts: List[str] = [".*"]
    deny_hosts: List[str] = []
    allow_commands: List[CommandWhitelistConfig]


# ── Request / Response ───────────────────────────────────────────────────────

class CommandOption(BaseModel):
    timeout_seconds: int = 30
    bastion_type: Optional[str] = None
    ip_label: Optional[str] = None


class CommandExecutionRequest(BaseModel):
    command_name: str
    host: str
    host_type: HostType = HostType.IP
    port: int = 22
    username: str
    ssh_config: str = "default"
    option: Optional[CommandOption] = Field(default_factory=CommandOption)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    # Optional caller-supplied version floor, forwarded verbatim to
    # deploy-service (may only RAISE the whitelist minimum). Validation of the
    # semver string happens upstream on deploy-service.
    min_script_version: Optional[str] = None


class CommandExecutionResponse(BaseModel):
    command_id: Optional[str] = None
    status: str
    message: str = ""
    exit_status: Optional[int] = None
    output: Optional[str] = None
    exec_command: Optional[str] = None
    host_type: Optional[HostType] = None
    resolved_ip: Optional[str] = None
    pgids: List[int] = Field(default_factory=list)

    # ── Parsed output (populated only by GET /execution/{id}?format=json) ──
    # ``output`` above always keeps its raw string value; these are purely
    # additive so a caller that never passes ?format=json sees an unchanged
    # response. Both are produced upstream on deploy-service and forwarded
    # verbatim. ``output_json`` is typed ``Any`` because the source script may
    # legitimately emit an object, an array, or a scalar.
    output_json: Optional[Any] = None
    output_json_error: Optional[OutputJsonError] = None


class CommandLogLine(BaseModel):
    num: int
    content_html: str


class CommandTraceResponse(BaseModel):
    command_id: str
    status: str
    next_byte_offset: int
    next_line_num: int
    lines: List[CommandLogLine]
    total_size: int = 0
    size_warning: bool = False
    too_large: bool = False
    # True when the command was not run with ``logged: true``, so no run log
    # exists on deploy-service's control_node and the viewer has nothing to
    # stream. The UI shows an explanatory notice instead of polling forever.
    not_logged: bool = False
    log_host: Optional[str] = None
    log_port: Optional[int] = None
    log_user: Optional[str] = None
    log_file_path: Optional[str] = None
