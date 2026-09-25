---
name: software-factory
description: Run the owner's software factory from chat. Check and enable repos, plan and start issues, answer escalations.
version: 1.0.0
metadata:
  hermes:
    tags: [software-factory, github]
---

# Software factory

The factory takes a GitHub issue with checkable acceptance criteria and drives it to a reviewed,
merged PR. You reach it only through the `factory_*` tools. Hermes asks the owner to approve each
call that changes something, so do not also ask for a yes in chat.

## What needs the owner

Call factory_status with no id. First list the WorkItems in human_waiting, blocked or exhausted,
each with its id and blockedOn. Then list the others with their state and PR number. Report only
what factory_status returns.

## Enable a repo

1. Call factory_repo_check with the repo as owner/repo.
2. If `ready` is false, give each failed check's detail and fix, and stop.
3. If `enabled` is true, say so and stop.
4. Call factory_add_repo with the repo, and the owner's name as `by`.

## Plan work

Turn the owner's goal into issues the factory can finish:

- Each issue fits one PR: at most 600 changed lines and 20 files. Split a bigger goal into issues
  that each make sense alone.
- Each body ends with an `## Acceptance` heading and bullets of the form
  `- <claim> — done when: <check>`. A check is read from the result: a test that passes, a command
  that prints something, a file that contains something. Never "when it is done". Add one bullet
  for what must not change.
- No issue asks for changes under `.github/` or to `AGENTS.md`. Those need a person.

Show the owner the titles and acceptance first. Then call factory_propose_issue once per issue.
It opens the issue labelled `factory:proposed`; nothing starts yet.

## Start an issue

Call factory_approve_issue with the repo, the issue number, and the owner's name as `by`. It
labels the issue `factory:ready`, and the factory starts. If it refuses, give the reason word for
word. The usual causes are a missing `## Acceptance` list, an author the factory does not trust,
and a repo that is not enabled.

## Answer an escalation

A factory-watch alert names a WorkItem that waits on the owner. When the owner replies to one:

1. Call factory_status with the WorkItem id and read its escalation: `reason`, `needFromYou` and
   `tried`.
2. If the owner has not chosen, explain the choices. `retry` gives the worker another pass with
   the owner's note as a finding. `regate` sends a fix the owner pushed back to the gate. `close`
   gives up on the WorkItem.
3. Call factory_resolve_human with the id, the choice, the owner's own words as the note, and the
   owner's name as `by`. Never choose for the owner.

## Never

- Merge a PR, add or remove a label, or push to a `factory/*` branch. The gate merges, and
  factory-mcp pushes.
- Reach factory-mcp through the shell.
