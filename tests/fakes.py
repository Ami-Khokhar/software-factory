"""Shared test doubles and builders."""


def workitem(**fields):
    doc = {"schemaVersion": "factory.workitem/v1", "id": "o/r#1", "repo": "o/r", "issueNumber": 1,
           "goal": "g", "acceptance": [{"claim": "c", "doneWhen": "d"}], "state": "issued",
           "stateVersion": 0, "createdAt": "2026-09-24T00:00:00Z", "updatedAt": "2026-09-24T00:00:00Z"}
    doc.update(fields)
    return doc
