"""Publish saved dream plans without duplicate creates or unbounded auto-admission."""
import html
import json
from datetime import datetime, timezone

from . import dream_model, findings as ledger
from .mcp import ToolError

NEW_ISSUE_CAP = 3



NEXT_STEP = "Add `factory:ready` to have the factory do it, or comment with what to change."


def body(candidate, run, owner=None):
    owner = owner or candidate["repo"].split("/")[0]
    escape = lambda value: html.escape(value).replace("`", "&#96;")
    refs = {dream_model.source_id(event): event for event in run["evidence"]}
    lines = ["## Goal", escape(candidate["summary"]), "", "Paths: " + ", ".join(candidate["paths"]),
             "", "## Evidence"]
    for ref in candidate["evidence"]:
        event = refs[ref]
        number = event["workItemId"].rsplit("#", 1)[-1]
        lines.append(f"- {ref}: https://github.com/{event['repo']}/issues/{number} ({event['action']})")
    lines += ["", "## Acceptance"]
    lines += [f"- {escape(check['claim'])} — done when: {escape(check['command'])}" for check in candidate["checks"]]
    lines += ["- Existing credentials, protected paths and merge controls remain unchanged — done when: "
              "the repository verify command passes and protected paths are unchanged.", "",
              f"@{owner}", NEXT_STEP, "",
              f"<!-- factory-dream:{candidate['fingerprint']} -->", f"Dream run: {run['id']}"]

    return "\n".join(lines)


def save(store, fingerprint, repo, doc, expected_validation=None):
    with store._lock, store._transaction():
        row = store._db.execute("SELECT doc FROM dream_issues WHERE fingerprint=?", (fingerprint,)).fetchone()
        current = json.loads(row[0]) if row else {}
        if expected_validation is not None and (current.get("validation") or {}).get("version", 0) != expected_validation:
            raise ToolError("measurement changed during maintenance; retry from current state")
        merged = {**current, **doc}
        if expected_validation is None and current.get("validation"):
            merged["validation"] = current["validation"]
            if "checkedHead" in current:
                merged["checkedHead"] = current["checkedHead"]
            else:
                merged.pop("checkedHead", None)
        if current.get("status") == "admitted" and doc.get("status") in ("proposed", "admission_reserved"):
            merged["status"] = "admitted"
        store._db.execute("INSERT INTO dream_issues VALUES (?,?,?) ON CONFLICT(fingerprint) "
                          "DO UPDATE SET doc=excluded.doc", (fingerprint, repo, json.dumps(merged)))
    return merged


def automatic_reason(foreman, candidate, issue, expected, run):
    config, store = foreman.config, foreman.store
    if candidate["repo"].lower() not in {repo.lower() for repo in config.dream_auto_repos}:
        return "automatic repairs are not enabled for this repo"
    if issue.get("state") != "open" or issue.get("body") != expected:
        return "existing issue is closed, edited, or a related follow-up; owner review required"
    if config.max_passes > 4:
        return "configured pass budget exceeds the automatic repair cap"
    for check in candidate["checks"]:
        if not check["command"].startswith(("python3 -m unittest", "python -m unittest", "pytest",
                                            "uv run pytest", "npm test", "go test", "cargo test")) or any(
                char in check["command"] for char in ";&|<>`$"):
            return "complex acceptance commands require owner review"
    if any(path.endswith(".md") for path in candidate["paths"]):
        return "documentation or instruction changes always wait for owner review"
    for path in candidate["paths"]:
        if path.split("/")[0] in (".github", ".git") or any(
                path == protected or protected.endswith("/") and path.startswith(protected)
                for protected in config.protected_paths) or any(
                part.startswith(".env") or part in ("auth.json", "credentials") or part.endswith((".pem", ".key"))
                for part in path.split("/")):
            return "protected or credential-related paths require owner review"
    with store._lock:
        earlier = [json.loads(row[0]) for row in store._db.execute("SELECT doc FROM dream_issues")]
    generated = {f"{row['repo']}#{row['issueNumber']}" for row in earlier if row.get("issueNumber")}
    refs = set(candidate["evidence"])
    if any(event["workItemId"] in generated for event in run["evidence"] if dream_model.source_id(event) in refs):
        return "an improvement failed; do not automatically spawn descendant repairs"
    return None


def reserve_admission(store, run_id, wid):
    from .workitem import TERMINAL
    with store._lock, store._transaction():
        rows = store._db.execute("SELECT run_id, workitem_id FROM dream_admissions").fetchall()
        if any(id_ == run_id for id_, _ in rows):
            return False
        for _, active in rows:
            found = store._db.execute("SELECT doc FROM workitems WHERE id=?", (active,)).fetchone()
            if found is None or json.loads(found[0])["state"] not in TERMINAL:
                return False
        store._db.execute("INSERT INTO dream_admissions VALUES (?,?)", (run_id, wid))
    return True


def priority(store):
    """Most reported causes first, then causes with the newest recorded evidence."""
    def key(candidate):
        finding = store.findings.get(store.findings.identity(candidate["repo"], candidate["fingerprint"]))
        if finding is None:
            return (0, 0)
        newest = ledger._ts(finding.get("lastSeen") or finding["at"]).timestamp()
        return (-finding.get("occurrences", 0), -newest)
    return key


def evidence_note(candidate, run):
    return "Same cause found again; evidence added, no new issue opened.\nEvidence: " + \
        ", ".join(candidate["evidence"]) + f"\nDream run: {run['id']}"


def publish(foreman, run):
    store, gh, config = foreman.store, foreman.github, foreman.config
    published, suppressed, deferred, cache = [], [], [], {}
    pending = False
    ordered = sorted(run["result"]["improvements"], key=priority(store))
    created = 0
    for candidate in ordered:
        repo, fingerprint = candidate["repo"], candidate["fingerprint"]
        if config.listed(repo) is None:
            raise ToolError("dream target repo is no longer enabled")
        finding = store.findings.get(store.findings.identity(repo, fingerprint))
        # A rejected finding silences its cause again; evidence the decision
        # postdates may come back, but only as a fresh proposal for the owner.
        if finding and finding.get("verdict") in ledger.TERMINAL:
            decision = finding.get("rejectedAt") or finding.get("at")
            if ledger._ts(finding.get("lastSeen") or finding["at"]) <= ledger._ts(decision):
                suppressed.append({"repo": repo, "fingerprint": fingerprint,
                                   "reason": f"the owner {finding['verdict']} this cause and the evidence is no newer"})
                continue
            fresh = True
        else:
            fresh = False

        # A fresh publication supersedes the old verdict: the next recurrence
        # must comment on an issue, not open another.
        with store._lock:
            row = store._db.execute("SELECT doc FROM dream_issues WHERE fingerprint=?", (fingerprint,)).fetchone()
        record = json.loads(row[0]) if row else None
        # An outstanding ambiguous create is always searched again, fresh or
        # not; a fresh cause supersedes any other record the fingerprint has,
        # but an owner-reopened issue that still carries its marker is adopted.
        searchable = record is None or record.get("status") == "creating" or fresh
        if record and record.get("issueNumber") and record.get("status") != "creating" and not fresh:
            if finding is not None and finding["occurrences"] > record.get("commentedOccurrences", 0):
                gh.comment(repo, record["issueNumber"], evidence_note(candidate, run))
                record["commentedOccurrences"] = finding["occurrences"]
                save(store, fingerprint, repo, record)
            if record.get("status") == "admission_reserved":
                existing = store.get(f"{repo}#{record['issueNumber']}")
                if existing is None:
                    pending = True
                else:
                    record["status"] = "admitted"
                    save(store, fingerprint, repo, record)
            published.append(record)
            continue
        if repo not in cache:
            cache[repo] = gh.all_issues(repo)
        # Rejected causes with newer evidence are never quietly re-adopted:
        # the closed issue stays closed and the fresh proposal speaks for itself;
        # only an open issue from an outstanding create may still be adopted.
        marker = f"<!-- factory-dream:{fingerprint} -->"
        matches = [] if not searchable else [
            issue for issue in cache[repo]
            if marker in (issue.get("body") or "") and (not fresh or issue.get("state") == "open")]
        # Exact related gate follow-ups can be reused, but are never auto-approved as new work.
        if not matches and searchable and not fresh:
            matches = [issue for issue in cache[repo] if issue.get("title", "").startswith("Follow-up to #")
                       and candidate["cause"].casefold() in (issue.get("body") or "").casefold()
                       and any(path in (issue.get("body") or "") for path in candidate["paths"])]
        if len(matches) > 1:
            raise ToolError("multiple matching improvement issues; owner review required")
        expected = body(candidate, run, config.owner or repo.split("/")[0])
        if matches:
            issue = matches[0]
            if fresh:
                # An adopted issue keeps the newer evidence that justified the
                # fresh publication, like the linked-finding branch above.
                gh.comment(repo, issue["number"], evidence_note(candidate, run))
        else:
            if created >= NEW_ISSUE_CAP:
                deferred.append({"repo": repo, "fingerprint": fingerprint,
                                 "reason": f"the cap of {NEW_ISSUE_CAP} new issues per run was reached"})
                continue
            creating = {"repo": repo, "status": "creating", "runId": run["id"]}
            with store._lock, store._transaction():
                if record is None:
                    inserted = store._db.execute("INSERT OR IGNORE INTO dream_issues VALUES (?,?,?)", (
                        fingerprint, repo, json.dumps(creating))).rowcount
                elif record.get("status") == "creating":
                    inserted = 0  # ambiguous prior create: never blindly retry
                elif fresh:
                    # The superseded publication is history, but its measured
                    # outcome and earlier issue still speak for this cause.
                    carried = {key: record[key] for key in ("validation", "checkedHead", "fixMemoryId")
                               if key in record}
                    if record.get("issueNumber"):
                        carried["previousIssue"] = record["issueNumber"]
                    store._db.execute("UPDATE dream_issues SET doc=? WHERE fingerprint=?",
                                      (json.dumps(creating | carried), fingerprint))
                    inserted = 1
                else:
                    inserted = 0
            if not inserted:
                pending = True
                continue
            made = foreman.propose_issue(repo, "Dream: " + candidate["cause"], expected)
            issue = gh.issue(repo, made["issue"])
            cache[repo].append(issue)
            created += 1
        if not config.trusts(repo, (issue.get("user") or {}).get("login")):
            raise ToolError("improvement issue author is not trusted")
        if fresh:
            store.findings.reopen(store.findings.identity(repo, fingerprint),
                                  "newer evidence outranks the earlier decision",
                                  finding["lastSeen"])
        record = {"repo": repo, "fingerprint": fingerprint, "issueNumber": issue["number"],
                  "runId": run["id"], "memoryId": candidate["memoryId"], "status": "proposed",
                  "paths": candidate["paths"],
                  "commentedOccurrences": finding["occurrences"] if finding else 0}
        reason = automatic_reason(foreman, candidate, issue, expected, run)
        wid = f"{repo}#{issue['number']}"
        if reason is None and reserve_admission(store, run["id"], wid):
            save(store, fingerprint, repo, {**record, "status": "admission_reserved"})
            foreman.approve_issue(repo, issue["number"], "dreaming")
            record["status"] = "admitted"
        else:
            record["reason"] = reason or "one admission per run; another repair is active or reserved"
        if finding is None:
            # A plan saved before the ledger existed still leaves its row behind now.
            started = datetime.fromtimestamp(run["startedAt"], timezone.utc)
            store.findings.record(repo, fingerprint, candidate["summary"], ",".join(candidate["evidence"]),
                                  started.isoformat(), issue=issue["number"])
            finding = store.findings.get(store.findings.identity(repo, fingerprint))
        else:
            store.findings.link(store.findings.identity(repo, fingerprint), issue["number"])
        record = save(store, fingerprint, repo, record)
        published.append(record)
    if not pending:
        with store._lock:
            current = store._db.execute("SELECT doc FROM dream_runs WHERE id=?", (run["id"],)).fetchone()
            doc = json.loads(current[0])
            doc.update(status="complete", publications=published, suppressed=suppressed,
                       deferred=deferred)
            store._db.execute("UPDATE dream_runs SET status='complete', doc=? WHERE id=?", (json.dumps(doc), run["id"]))
    return {"status": "publication_pending" if pending else "complete", "runId": run["id"],
            "issues": published, "suppressed": suppressed, "deferred": deferred}


def run(foreman, at):
    from .outcomes import refresh
    refresh(foreman, at)
    latest = foreman.store.dreaming.latest()
    if latest and latest["status"] == "planned":
        return publish(foreman, latest)
    result = dream_model.reflect(foreman.store, foreman.config, at)
    latest = foreman.store.dreaming.latest()
    return publish(foreman, latest) if result.get("status") == "planned" else result
