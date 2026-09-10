# Drain refuses before evicting, and slow termination is not a failure

Two decisions about `POST …/nodes/{node}/drain`, taken together because they
split one former behaviour — "the drain failed" — into two different answers.

## Protected pods refuse the whole drain, before anything is evicted

`force` and `delete_emptydir_data` had been declared on `DrainOptions` and
recommended in error messages, but were never read: drain removed unmanaged and
emptyDir pods unconditionally, as though both flags were permanently on. They
now guard, and a pod they guard refuses the entire drain up front.

**All-or-nothing was chosen over skipping the blocked pods.** Skipping would let
the endpoint report success while leaving the node occupied — and the pods it
did evict cannot be brought back, so the caller cannot undo the half-drain and
reconsider. Refusing costs a round-trip; skipping costs data with no way back.

**The refusal reports every blocker at once**, not the first one found. A caller
who learns one missing flag per attempt pays a round-trip per blocker, which is
the slow discovery this check exists to eliminate. Both reasons are collected
even for a single pod that breaks both rules.

**The node stays cordoned after a refusal.** Cordoning is harmless on its own,
is what the caller asked for, and leaving it means a corrected retry has nothing
to redo. Rolling it back would also be a lie about what happened.

Unconditional skips (DaemonSet, mirror, completed) are evaluated *before* the
guards, so a DaemonSet pod using emptyDir does not demand `delete_emptydir_data`
for a pod that was never going to be touched.

## Exceeding the wait budget returns 200, not 504

Drain previously raised `DrainTimeoutException` → 504 when pods outlived
`DRAIN_DEFAULT_TIMEOUT_SECONDS`. That was the wrong shape twice over. Nothing
had timed out in the gateway sense the status code implies — every eviction was
accepted by the API server. And a pod with a long `terminationGracePeriodSeconds`
outliving a 25-second budget is behaving exactly as its author configured it;
calling that an error makes correct configuration look like a fault.

Drain now returns 200 with `still_terminating`, `node_emptied`, and
`forced_deletion`. The wait budget bounds the *reporting*, not the *outcome*:
spending it changes what the response says, never what was asked of the cluster.

**The 504 was rejected rather than kept alongside** because the caller's next
action is identical in both cases — look at what is still running and decide
whether to wait — and an error status pushes that into an exception handler for
something that is not exceptional. `DrainTimeoutException` is deleted; its
escalation logic became the refusal's `required_options`.

## Consequences

`DRAIN_TIMEOUT` is gone from `ErrorCode` and `DRAIN_BLOCKED` (400) replaces it.
A caller that treated 504 as retry-worthy now sees 200 and must branch on
`node_emptied` instead — the field exists so that branch is one boolean rather
than a list-length test.

The deadline in `_wait_for_pods_gone` is checked *after* each poll, so a pod
that terminates on the final observation counts as drained rather than being
misreported as stuck.

`grace_period_seconds` gained `ge=0`. A negative value is always a caller bug,
and rejecting it in the model spends no Kubernetes round-trip to learn the same.

Verifying this against a real cluster is not optional: the behaviours that
matter — the Eviction API answering 429 under a PodDisruptionBudget, emptyDir
volumes appearing where the code looks for them, a SIGTERM-trapping pod really
outliving the budget — are assumptions about Kubernetes, and a mocked
`CoreV1Api` will confirm whatever the code already believes. Hence the `e2e`
suite in `tests/e2e/`, excluded from `make test` and run by `make test-e2e`.
