"""Publish saved dream plans without duplicate creates or unbounded auto-admission."""
import html
import json

from . import dream_model
from .mcp import ToolError



def body(candidate, run):
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
              f"<!-- factory-dream:{candidate['fingerprint']} -->", f"Dream run: {run['id']}"]
    return "\n".join(lines)


def save(store, fingerprint, repo, doc, expected_validation=None):
    with store._lock, store._transaction():
        row = store._db.execute("SELECT doc FROM dream_issues WHERE fingerprint=?", (fingerprint,)).fetchone()
        current = json.loads(row[0]) if row else {}
        if expected_validation is not None and (current.get("validation") or {}).get("version", 0) != expected_validation:
            raise ToolError("measurement changed during maintenance; retry from current state")
        merged = {**current, **doc}
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


def publish(foreman, run):
    store, gh, config = foreman.store, foreman.github, foreman.config
    published, pending, cache = [], False, {}
    for candidate in run["result"]["improvements"]:
        repo, fingerprint = candidate["repo"], candidate["fingerprint"]
        if config.listed(repo) is None:
            raise ToolError("dream target repo is no longer enabled")
        with store._lock:
            row = store._db.execute("SELECT doc FROM dream_issues WHERE fingerprint=?", (fingerprint,)).fetchone()
        record = json.loads(row[0]) if row else None
        if record and record.get("issueNumber"):
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
        marker = f"<!-- factory-dream:{fingerprint} -->"
        matches = [issue for issue in cache[repo] if marker in (issue.get("body") or "")]
        # Exact related gate follow-ups can be reused, but are never auto-approved as new work.
        if not matches:
            matches = [issue for issue in cache[repo] if issue.get("title", "").startswith("Follow-up to #")
                       and candidate["cause"].casefold() in (issue.get("body") or "").casefold()
                       and any(path in (issue.get("body") or "") for path in candidate["paths"])]
        if len(matches) > 1:
            raise ToolError("multiple matching improvement issues; owner review required")
        expected = body(candidate, run)
        if matches:
            issue = matches[0]
        else:
            with store._lock, store._transaction():
                inserted = store._db.execute("INSERT OR IGNORE INTO dream_issues VALUES (?,?,?)", (
                    fingerprint, repo, json.dumps({"repo": repo, "status": "creating", "runId": run["id"]}))).rowcount
            if not inserted:
                pending = True  # ambiguous prior create: search again later, never blindly retry
                continue
            made = foreman.propose_issue(repo, "Dream: " + candidate["cause"], expected)
            issue = gh.issue(repo, made["issue"])
            cache[repo].append(issue)
        if not config.trusts(repo, (issue.get("user") or {}).get("login")):
            raise ToolError("improvement issue author is not trusted")
        record = {"repo": repo, "fingerprint": fingerprint, "issueNumber": issue["number"],
                  "runId": run["id"], "memoryId": candidate["memoryId"], "status": "proposed",
                  "paths": candidate["paths"]}
        reason = automatic_reason(foreman, candidate, issue, expected, run)
        wid = f"{repo}#{issue['number']}"
        if reason is None and reserve_admission(store, run["id"], wid):
            save(store, fingerprint, repo, {**record, "status": "admission_reserved"})
            foreman.approve_issue(repo, issue["number"], "dreaming")
            record["status"] = "admitted"
        else:
            record["reason"] = reason or "one admission per run; another repair is active or reserved"
        record = save(store, fingerprint, repo, record)
        published.append(record)
    if not pending:
        with store._lock:
            current = store._db.execute("SELECT doc FROM dream_runs WHERE id=?", (run["id"],)).fetchone()
            doc = json.loads(current[0])
            doc.update(status="complete", publications=published)
            store._db.execute("UPDATE dream_runs SET status='complete', doc=? WHERE id=?", (json.dumps(doc), run["id"]))
    return {"status": "publication_pending" if pending else "complete", "runId": run["id"], "issues": published}


def run(foreman, at):
    from .outcomes import refresh
    refresh(foreman, at)
    latest = foreman.store.dreaming.latest()
    if latest and latest["status"] == "planned":
        return publish(foreman, latest)
    result = dream_model.reflect(foreman.store, foreman.config, at)
    latest = foreman.store.dreaming.latest()
    return publish(foreman, latest) if result.get("status") == "planned" else result
