# State: quiet progress and update notifications

- Goal: make scheduled factory watch/update output quiet when nothing material changed while preserving owner blockers and real milestones.
- Done when: regressions cover first baseline, material transitions, updatedAt churn, update failure status/deduplication, unchanged deployed HEAD, and retries of incomplete deployment steps; full verify passes; issue and PR are published.
- Done: issue #319 has no labels; commit `74c509e` is pushed on `codex/milo-progress-notifications`; PR #320 is open and attached to this task; full verify passed (778 tests).
- Next: parent monitors gatekeeper review.
- Blockers: none.
