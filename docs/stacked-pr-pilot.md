# The stacked pull-request pilot (issue #227) — specification, not production

Status: **factory:proposed**. Activation is owner-approved and needs evidence that the full
recovery implementation, integrated verification and the agreed representative rollout are
complete first (ADR-0072). This document specifies a guarded two-issue stack; the executable
demonstration is `scripts/stack_pilot`, which rehearses both layers in bare repositories that
exist only inside a scratch directory and touch no production system.

The pilot reuses what the factory already has and adds no orchestration framework:

- One issue is one pull request (ADR-0002): a stack is two such pull requests, each with its own
  issue, worker, gate review and merge.
- The gate is the only routine merger (ADR-0004). A stack changes the *order* of merges,
  bottom-up; it never changes *who* merges or how a merge is authorised.
- The `WorkOrder`/`Result` `baseSha` pin (schemas/workorder.schema.json,
  schemas/result.schema.json) is the exact parent head a layer was built on.
- pr-gatekeeper's latest review comment is the only accepted evidence; a stack layer carries the
  same Verdict shape and limits as any pull request.
- The gate's squash merge (the only merge kind the gate performs) fixes what "the parent merged"
  means: the squash commit the parent produced, not its final review head.

## Rules of a two-layer stack

1. **Exact parent pinning.** The child is built on the parent's exact head sha, recorded as its
   base, like `baseSha` is today. A child is never built on "~the branch".
2. **Per-layer size and evidence.** Each layer satisfies the full one-issue rules from
   `AGENTS.md` — separately measured at `git diff --shortstat` from its own merge base, gate
   review on its own pull request. The standard's 600 lines stay advisory (ADR-0070).
3. **Integrated size and verification.** The open layers together are measured like one pull
   request (`pilot.check_stack`): the sum must fit the 600-line, 20-file budget, and the whole
   stack is verified once integrated, not layer by layer only.
4. **Stale evidence on parent change.** If the parent's live head differs from the child's pin,
   or the parent merges at a squash sha different from the child's pin, the child's review
   evidence is stale (`pilot.check_child` → `stale`): the child is rebased and re-reviewed before
   anything merges.
5. **Sandboxed conflict repair.** Rebase happens in a throwaway clone (`pilot.repair`), never in
   a worker or gate checkout. A layer that does not rebase cleanly is reworked, not forced.
6. **Bottom-up, gate-only merges.** The child may merge only when the parent is merged, the
   child's evidence cites its own head sha and a base equal to the parent's squash sha, and the
   child's latest gate verdict is MERGE. No human or tool merges the child's work while the
   parent still gates.
7. **Pause on a failed or cancelled parent.** A parent whose WorkOrder failed or was cancelled
   pauses the child (`pause`): the child is not promoted, re-reviewed or closed, and waits for
   the owner. The pause is on the parent state, so a resumable parent (see the recovery work)
   resumes the child with it.

## What the fixture demonstrates

`python3 scripts/stack_pilot <scratch-dir>` builds one bare repository and one working clone in
the scratch directory, then runs both layers through:

1. a fresh child under an open parent (hold: the parent gates first);
2. a parent change, which the policy calls stale, repaired by a sandboxed rebase onto the new
   parent head;
3. a failed parent, which pauses the child and lets nothing reach main;
4. the parent's squash merge, which stales the child's pin until it re-runs on the squash sha;
5. the integrated size check across the open layers and the bottom-up merges, both layers ending
   on main.

`tests/test_stack_pilot.py` pins every rule above through the policy and runs the fixture. The
fixture only uses local git: it displays how the guard behaves and never dispatches, merges,
opens or changes anything outside its scratch directory.

## Handoff

Implementation arrives later as separate reviewed issues, each within the one-issue limits
(<=600 changed lines, <=20 files), never broadening dispatch or weakening the existing
dependency guards. The controls this pilot must preserve unchanged: the merge authority of
pr-gatekeeper (ADR-0004), the owner's protected controls and Day Desk's per-token tool split
(ADR-0030, ADR-0041), and the worker's credential isolation.
