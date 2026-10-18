# Worker operations foundation

Goal: add bounded, sanitized worker health checks and a fixed relay reload operation before
worker activation.

Done when: issue #313 acceptance passes, the full verify suite passes, a pull request is open,
and the changed paths stay within the repository limits.

Done: issue #313 exists without a factory:ready label. Implement guarded health validation,
sanitized child probing, durable health/quarantine records, and the fixed relay reload tool.

Next: finish behavior tests and docs, run serialized full verification, then open a PR.

Out of scope for this slice: activation of any worker, dispatch-preflight integration, and live
factory settings changes.

Blockers: none.
