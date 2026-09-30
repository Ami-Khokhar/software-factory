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
2. If gate files or the factory-standard block are missing, call factory_bootstrap_repo with the
   repo and the owner's name as `by`. When the gate config is missing, ask the owner for its
   test/verify command and pass it as `verify`. The tool opens a setup PR containing only the
   missing controls. Tell the owner to review and merge that PR; do not start work before it is
   merged. If the tool refuses, report its reason word for word.
3. Call factory_repo_check again after the owner merges the setup PR. If `ready` is false, give
   each failed check's detail and fix, and stop.
4. If `enabled` is true, say so and stop. Otherwise call factory_add_repo with the repo and the
   owner's name as `by`.

## Plan work

Turn the owner's goal into issues the factory can finish, as the Plan rules of the factory
standard in the repo's AGENTS.md ask:

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

## Teach the factory a lesson

A lesson is a gate finding the factory saw on two or more WorkItems in one repo, recorded in
the factory's lessons file with the owner's words. Only the owner approves one. Workers do not see
lessons yet; say so if the owner asks.

1. Call factory_lessons with the repo. Show each candidate with its problem, its fingerprint and
   how many WorkItems it blocked, then the lessons already approved.
2. If the owner picks a candidate, ask how they want the worker to read it: one line, at most 300
   characters. Do not write the text for them.
3. Call factory_approve_lesson with the repo, the fingerprint, the owner's own words as `text`, and
   the owner's name as `by`. If it refuses, give the reason word for word.
4. To stop a lesson, call factory_retire_lesson with the repo, its fingerprint and the owner's
   name as `by`.

## Never

- Merge a PR, add or remove a label, or push to a `factory/*` branch. The gate merges, and
  factory-mcp pushes.
