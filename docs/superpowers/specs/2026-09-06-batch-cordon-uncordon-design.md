# Batch Cordon / Uncordon API — Design

Date: 2026-09-06
Status: Ready for implementation

## Problem Statement

An operator preparing a cluster for maintenance needs to take several nodes out
of the scheduler at once. Today the only way to do that is to call
`POST /api/v1/clusters/{cluster}/nodes/{node}/cordon` once per node — eight
nodes means eight HTTP round-trips, eight chances to lose track of which call
failed, and no single record in the logs of "this maintenance window cordoned
these eight machines". The same applies in reverse when bringing the nodes back.

The per-node loop also pushes the hard part onto every caller. Each caller has
to decide independently what to do when node three of eight does not exist:
stop, continue, retry, roll back. Different callers make different choices, and
none of those choices are visible to the service.

## Solution

Two new endpoints that accept a list of node names for one cluster and apply
`cordon` (or `uncordon`) to each of them, returning a per-node result for every
node in the request.

The request always reports what happened to every node. A node that failed does
not hide the nodes that succeeded, and a node that succeeded does not imply the
whole batch did. Callers read one `summary` object to decide whether to care,
and the `results` array to decide what to do about it.

Cordon and uncordon are cheap (a single `spec.unschedulable` patch each) and
idempotent, so the batch is a convenience and an audit surface — not a
long-running job. It stays a synchronous request/response with no job id, no
polling, and no state to persist.

## User Stories

1. As a cluster operator, I want to cordon several nodes in one call, so that a
   maintenance window starts with one action instead of eight.
2. As a cluster operator, I want to uncordon several nodes in one call, so that
   returning capacity after maintenance is as cheap as removing it.
3. As a cluster operator, I want the response to list every node I asked about,
   so that I never have to infer what happened to a node from its absence.
4. As a cluster operator, I want a summary of totals, so that I can branch on
   one field instead of scanning the whole results array.
5. As a cluster operator, I want a partial failure to still apply the nodes that
   could be applied, so that one bad node name does not block a maintenance
   window.
6. As a cluster operator, I want each failed node to carry a machine-readable
   error code, so that my automation can distinguish "no such node" from "the
   API server rejected it".
7. As a cluster operator, I want each failed node to carry the underlying
   Kubernetes HTTP status, so that my automation knows whether retrying is
   worthwhile — a 503 is worth retrying, a 403 is not.
8. As a cluster operator, I want a failed node to carry a human-readable
   message, so that I can read the response directly without decoding it.
9. As a cluster operator, I want an unreachable cluster to fail the whole
   request rather than appear as N identical node failures, so that I can tell
   "the cluster is down" apart from "these nodes are bad".
10. As a cluster operator, I want a nonexistent cluster to be rejected outright,
    so that I do not get a response full of misleading per-node errors.
11. As a cluster operator, I want to attach a reason to a batch operation, so
    that the log explains why the capacity was removed.
12. As an on-call engineer, I want the log line to name the user who ran the
    batch, so that I can find out who cordoned these machines.
13. As an on-call engineer, I want the log line to name the nodes that failed,
    so that the log answers the question without me digging up the original
    response body.
14. As an on-call engineer, I want the log line to carry requested/succeeded/
    failed counts, so that I can spot a partially-applied batch at a glance.
15. As an on-call engineer, I want the batch log line correlated by request id,
    so that it joins up with the rest of the request's logs.
16. As an API consumer, I want a duplicated node name to be collapsed, so that I
    can safely index the results array by node name.
17. As an API consumer, I want an empty node list rejected, so that a bug in my
    caller surfaces instead of silently succeeding.
18. As an API consumer, I want an oversized batch rejected with a clear
    validation error, so that I learn the limit before I send the request.
19. As an API consumer, I want the limit visible in the OpenAPI schema, so that
    I know it without reading the service source.
20. As an API consumer, I want the two actions on separate endpoints, so that
    the operation I invoked is legible in an access log without parsing a body.
21. As an API consumer, I want the batch response to reuse the service's
    standard envelope, so that my existing response handling applies unchanged.
22. As an API consumer, I want success and failure entries to share one shape,
    so that my generated client is not forced into an awkward union type.
23. As a platform operator, I want the batch endpoints gated by the same scope
    as the single-node ones, so that access control does not change meaning.
24. As a platform operator, I want a large batch not to stall unrelated
    requests, so that health checks keep answering during a maintenance window.
25. As a platform operator, I want an upper bound on batch size, so that an
    authenticated caller cannot consume the service's worker capacity with one
    request.
26. As a developer, I want the batch loop to live in the service layer, so that
    the router stays a thin HTTP boundary as the architecture requires.
27. As a developer, I want cordon and uncordon to share their batch
    implementation, so that a fix to one cannot drift away from the other.
28. As a developer, I want the per-node error classification tested at the
    service seam, so that I can cover failure modes without an HTTP client.
29. As a developer, I want the request validation tested through the router, so
    that the Pydantic constraints are actually exercised.
30. As a developer, I want the batch to reuse the existing error code
    vocabulary, so that clients do not have to learn new codes for old failures.

## Implementation Decisions

### API contract

Two endpoints, cluster-scoped, colon-verb form:

```text
POST /api/v1/clusters/{cluster}/nodes:cordon
POST /api/v1/clusters/{cluster}/nodes:uncordon
```

Both require the `cluster_api` scope, matching the single-node node routes.

The colon-verb form is new to this repo — every existing route uses path
segments. It is introduced deliberately: `nodes/cordon` reads as "the node named
cordon", whereas `nodes:cordon` marks a custom action on the collection. The
form is confined to these batch actions and is not a general migration.

Request body:

```json
{"nodes": ["node-a", "node-b", "node-c"], "reason": "monthly maintenance"}
```

- `nodes` — required, `min_length=1`, `max_length=100`. Violations are rejected
  by Pydantic as 422, so the bound appears in the OpenAPI schema.
- `reason` — optional free text. Logged only; never sent to Kubernetes.

Duplicate node names are silently de-duplicated before execution, preserving
first-seen order. `summary.total` counts the de-duplicated list, so `results` is
safe to key by node name.

Response — always HTTP 200 whenever the cluster itself was reachable:

```json
{"data": {"cluster": "c1",
          "action": "cordon",
          "summary": {"total": 3, "succeeded": 2, "failed": 1},
          "results": [
            {"node": "node-a", "status": "success"},
            {"node": "node-b", "status": "success"},
            {"node": "node-c", "status": "failed",
             "error_code": "NODE_NOT_FOUND",
             "message": "Node 'node-c' not found in cluster 'c1'.",
             "kube_status": 404}]},
 "request_id": "..."}
```

HTTP status describes the request, not the nodes. 207 Multi-Status was rejected:
proxies and HTTP clients handle it inconsistently, and the outcome is already
fully described in a body the service controls.

### Error layering

Two distinct failure categories, deliberately kept apart:

- **Request-level** — the cluster is not registered, its credentials are
  invalid, or the API server is unreachable. These propagate as exceptions
  through the existing global handler and produce the service's standard 4xx/5xx
  error envelope. No `results` array is returned, because no node was attempted.
- **Node-level** — the cluster was reached, but this particular node's patch
  failed. These are captured per node into `results` and never abort the batch.

### Models

New Pydantic models alongside the existing Kubernetes domain models:

- `BatchNodeRequest` — `nodes` and `reason`, carrying the length constraints.
- `BatchNodeResult` — one entry type for both outcomes:
  `{node, status, error_code?, message?, kube_status?}`. Success entries leave
  the three error fields unset. A union of success/failure types was rejected
  because it generates an awkward `anyOf` in OpenAPI clients.
- `BatchSummary` — `{total, succeeded, failed}`.
- `BatchNodeActionData` — `{cluster, action, summary, results}`. Fields constant
  across the batch (`cluster`, `action`) are hoisted here rather than repeated
  in every result entry.

Returned inside the existing `ApiResponse[T]` envelope.

The existing `ErrorCode` vocabulary is reused unchanged — `NODE_NOT_FOUND` and
`KUBE_API_ERROR` already cover every per-node failure. No new codes.

### Service layer

`NodeService` gains a private helper carrying the whole batch loop, parametrised
by the target schedulability and the action name. `cordon_many` and
`uncordon_many` are thin wrappers over it. This mirrors the existing structure,
where `cordon` and `uncordon` both delegate to a shared `_patch_unschedulable`
helper, and prevents the two batch paths from drifting.

The helper iterates the de-duplicated node list, calling the existing
single-node patch path per node, catching `NodeNotFoundException` and
`KubeApiException` and converting each into a failed `BatchNodeResult`. Any
other exception type is left to propagate — an unexpected error is not silently
downgraded to a per-node failure.

Execution is **sequential** within the batch. Kubernetes offers no transaction
across N node patches, so concurrency would buy latency at the cost of extra
API-server load and thread-pool pressure for an operation that is already
cheap.

### Concurrency

The routers await the service call via a thread offload rather than calling it
inline. `NodeService` is fully synchronous — it uses the blocking `kubernetes`
client — while the routers are `async def`. Calling it directly blocks the event
loop for the duration of the whole batch, during which the process serves no
other request, including health checks. Offloading to a worker thread keeps the
loop free while the batch runs sequentially on that thread.

This is applied to the two new routes only. The existing node routes have the
same defect and are left untouched here; see Further Notes.

### Logging

Each batch logs a single line at info level on completion, carrying: the acting
user, cluster, action, requested/succeeded/failed counts, the list of failed
node names, and the reason. Request-id correlation comes free from the existing
request-id middleware.

Naming the user is new for this router — the existing node routes inject the
current user for scope enforcement but never log it. The addition is scoped to
the two new batch routes; retrofitting the existing seven is out of scope.

## Testing Decisions

A good test here asserts on externally visible behaviour: the shape and content
of what the service returns, and the status code the router produces. It does
not assert how many times an internal helper was invoked, nor reach into private
attributes. The tests should survive the batch loop being rewritten, as long as
the contract holds.

Two seams, both already established in this codebase — no new seam is
introduced.

### Seam 1 — service unit tests

`NodeService` receives its `CoreV1Api` as a parameter, so tests inject a fake
shaped like that API. This is the pattern every existing node service test uses,
and `CLAUDE.md` explicitly requires it over patching the `kubernetes` SDK.

Cases: all nodes succeed; a mix of success and failure; a node that does not
exist becomes a `NODE_NOT_FOUND` entry with `kube_status` 404; an API failure
becomes a `KUBE_API_ERROR` entry carrying the underlying status; duplicates are
collapsed and reflected in `summary.total`; `summary` counts agree with the
`results` array; the patch is applied with the correct schedulability value for
each action; a cluster-level connection failure propagates as an exception
rather than becoming per-node failures.

Prior art: the existing cordon, uncordon and drain service tests, including the
ones asserting that a 404 raises `NodeNotFoundException` and a 500 raises
`KubeApiException`.

### Seam 2 — router integration tests

A `TestClient` against the full app, with the service and cluster-repository
providers replaced through FastAPI's dependency overrides. Prior art: the
inventory route tests, which override their service provider the same way.

These cover what unit tests structurally cannot — the Pydantic constraints and
the serialised envelope: an empty node list is 422; a list over the limit is
422; a valid batch is 200 with the documented body shape; a partial failure is
still 200; a missing or insufficient scope is rejected.

One wrinkle: the node routes construct their Kubernetes client inline rather
than receiving it through dependency injection, so these tests must also
intercept that construction. This is accepted as-is; refactoring the client
factory into the dependency graph is out of scope.

No end-to-end tests against a live cluster. Per the repo's CI convention such
tests must be marked `e2e` or they break CI, and the logic here is too thin to
justify the maintenance cost.

## Out of Scope

- **Cross-cluster batches.** One cluster per request. Cluster is a path segment
  because the Kubernetes client is built per cluster; a cross-cluster batch adds
  a second error dimension for no established workflow. Callers loop if needed.
- **Mixed-action batches.** No per-node `action` field. A batch that both cordons
  and uncordons is hard to read in an audit log and has no demonstrated need.
- **`dry_run`.** Rejected deliberately. A meaningful dry run would have to read
  every node first, doubling the API calls, and cordon is idempotent and
  instantly reversible — the value is far lower than it is for drain. Note the
  existing drain `dry_run` short-circuits without contacting the cluster, so
  copying it would produce an endpoint that always reports success.
- **Batch drain.** Drain is long-running and needs a different execution model.
- **207 Multi-Status.**
- **Concurrent execution within a batch.**
- **Retrofitting the existing seven node routes** with thread offloading or user
  logging.
- **Refactoring `KubeClientFactory`** into the dependency graph.

## Further Notes

The existing node routes run synchronous Kubernetes calls directly inside
`async def` handlers, blocking the event loop for the duration of every call.
This is a real, pre-existing defect, not one introduced here — the batch work
merely makes it more visible, since a batch blocks for N round-trips instead of
one. The two new routes avoid it via thread offloading; the existing routes
should be fixed under a separate change so this one stays scoped.

Cordon and uncordon are naturally idempotent: patching `spec.unschedulable` to a
value it already holds succeeds and is a no-op. Callers may safely retry a whole
batch after a partial failure without special handling.
