"""Break each code path its test covers, run that test, see it fail, restore the code.

The factory standard requires this step for a behavior change (templates/AGENTS.md, "Test").
Running it prints every mutation and the verbatim test run it caused. Exit 0 means every
mutation was caught by its test; exit 1 means a test passed over broken code; exit 2 means a
mutation target no longer matches, so the script is stale.

    python3 scripts/mutation_proof.py

Each mutation edits the file in place and restores it in a finally. An interrupt (Ctrl-C)
still restores; only a kill between the two writes leaves the edit behind, which `git
checkout -- <path>` undoes.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# path, the exact code the mutation replaces, what replaces it, the test that must fail.
MUTATIONS = (
    ("factory_mcp/registry.py",
     'return dict(template, id=worker_id,',
     'return dict(template, cwd="/tmp/evil", id=worker_id,',
     "tests.test_registry.RegistryUnitTest.test_build_agent_copies_every_field_but_the_model"),
    ("factory_mcp/foreman.py",
     'body = workitem.with_workers(issue.get("body"), workers)',
     'body = issue.get("body") or ""',
     "tests.test_workers.WorkerTest.test_set_worker_edits_the_issue_and_the_next_tick_routes_to_it"),
    ("factory_mcp/foreman.py",
     'if self._worker_live(wi):\n'
     '                raise ToolError(f"{workitem_id} has a worker running now; wait for it to finish "',
     'if False:\n'
     '                raise ToolError(f"{workitem_id} has a worker running now; wait for it to finish "',
     "tests.test_workers.WorkerTest.test_set_worker_refuses_while_a_worker_is_running"),
    ("factory_mcp/foreman.py",
     'if wi["state"] in workitem.TERMINAL:\n'
     '                raise ToolError(f"{workitem_id} is {wi[\'state\']}; its worker no longer matters")',
     'if False:\n'
     '                raise ToolError(f"{workitem_id} is {wi[\'state\']}; its worker no longer matters")',
     "tests.test_workers.WorkerTest.test_set_worker_refuses_a_terminal_workitem"),
    ("factory_mcp/foreman.py",
     'for key in ("fingerprint", "path", "line", "problem", "fix", "test")}',
     'for key in ("fingerprint", "path", "line", "problem")}',
     "tests.test_gate_loop.GateLoopTest."
     "test_a_changes_finding_with_fix_and_test_reaches_the_fix_workorder"),
    ("factory_mcp/foreman.py",
     'except patches.PatchError as exc:  # a refused patch is no partial to keep',
     'except RuntimeError as exc:  # a refused patch is no partial to keep',
     "tests.test_autonomous_recovery.SalvageTest."
     "test_a_partial_touching_a_protected_path_is_not_kept"),
)


def main():
    caught, stale = [], []
    for path, find, replace, test in MUTATIONS:
        file = ROOT / path
        source = file.read_text(encoding="utf-8")
        if source.count(find) != 1:
            stale.append(f"{path}: target found {source.count(find)} times, expected 1: {find[:60]!r}")
            continue
        print(f"== mutation in {path}: {find.splitlines()[0][:70]!r} -> {replace.splitlines()[0][:70]!r}",
              flush=True)
        try:
            file.write_text(source.replace(find, replace), encoding="utf-8")
            run = subprocess.run([sys.executable, "-m", "unittest", test], cwd=ROOT)
        finally:
            file.write_text(source, encoding="utf-8")
        if run.returncode == 0:
            print(f"NOT CAUGHT: {test} passes over broken {path}", flush=True)
        else:
            print(f"caught: {test} failed with exit {run.returncode}", flush=True)
            caught.append(test)
    for problem in stale:
        print(f"STALE: {problem}", file=sys.stderr)
    return 0 if len(caught) == len(MUTATIONS) else 1


if __name__ == "__main__":
    sys.exit(main())
