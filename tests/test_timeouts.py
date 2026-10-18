"""Per-item timeout changes preserve pass accounting and dispatch the exact approved budget."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from jsonschema import Draft202012Validator

from factory_mcp import documents, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay

ID = 'o/r#7'
ROOT = Path(__file__).resolve().parent.parent


class TimeoutTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue('o/r', 7)
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.config = Config(mcp_token='t', repos=frozenset({'o/r'}), wall_ms=1800000)
        self.foreman = Foreman(self.store, self.github, self.config, self.relay)
        self.foreman.intake('o/r', 7)

    def set_timeout(self, value, **extra):
        return tools.build(self.store, self.foreman, agent=True)['factory_set_timeout'].fn(
            dict(id=ID, wallMs=value, by='owner', **extra))['workItem']

    def test_set_is_audited_and_does_not_retry_or_spend(self):
        before = self.store.put(dict(self.store.get(ID), state='blocked', blockedOn='timed out',
                                     attempts=['failed'], passesBefore=2))
        wi = self.set_timeout(3600000)
        for key in ('state', 'blockedOn', 'attempts', 'passesBefore'):
            self.assertEqual(wi[key], before[key])
        self.assertEqual(wi['wallMsOverride'], 3600000)
        self.assertEqual(self.store.workorders(ID), [])
        self.assertEqual(self.relay.tasks, {})
        event = self.store.history(ID)[-1]
        self.assertEqual(event['action'], 'timeout_changed')
        self.assertEqual(event['detail'], {'by': 'owner', 'was': None, 'now': 3600000,
                                           'effectiveWallMs': 3600000, 'agent': True})

    def test_implement_and_fix_orders_and_relay_use_the_override(self):
        wi = self.set_timeout(3600000)
        self.foreman.tick(ID)
        order = self.store.workorders(ID)[0]
        task = next(iter(self.relay.tasks.values()))
        self.assertEqual(order['budget']['wallMs'], 3600000)
        self.assertEqual(task['timeoutMs'], 3600000)
        self.assertEqual(json.loads(task['input'].split('WorkOrder:\n')[1])['budget']['wallMs'], 3600000)
        self.assertEqual(documents.workorder(wi, 'fix', 0, self.config)['budget']['wallMs'], 3600000)
        self.assertEqual(order['budget']['maxPasses'], self.config.max_passes)

    def test_fix_dispatch_submits_the_same_persisted_budget(self):
        wi = self.set_timeout(3600000)
        wi = self.store.put(dict(wi, state='changes_requested', headSha='reviewed', gateRound=1))
        self.foreman.dispatch(wi, 'fix')
        order = self.store.workorders(ID)[0]
        task = next(iter(self.relay.tasks.values()))
        self.assertEqual((order['kind'], order['headSha']), ('fix', 'reviewed'))
        self.assertEqual((order['budget']['wallMs'], task['timeoutMs']), (3600000, 3600000))

    def test_clear_and_legacy_items_use_the_unchanged_default(self):
        self.set_timeout(3600000)
        wi = self.set_timeout(None)
        self.assertIsNone(wi['wallMsOverride'])
        for doc in (wi, {k: v for k, v in wi.items() if k != 'wallMsOverride'}):
            self.assertEqual(documents.workorder(doc, 'implement', 0, self.config)['budget']['wallMs'],
                             self.config.wall_ms)
        self.assertEqual(self.config.wall_ms, 1800000)

    def test_malformed_out_of_range_and_extra_arguments_do_not_mutate(self):
        for value in (True, False, 3600000.0, '3600000', {}, [], -1, 0, 59999, 60000, 1799999, 3600001):
            with self.subTest(value=value):
                before, history = self.store.get(ID), self.store.history(ID)
                with self.assertRaises(ToolError):
                    self.set_timeout(value)
                self.assertEqual(self.store.get(ID), before)
                self.assertEqual(self.store.history(ID), history)
        with self.assertRaises(ToolError):
            self.set_timeout(3600000, retry=True)
        for by in ('', ' ', None, 5):
            with self.assertRaises(ToolError):
                self.foreman.set_timeout(ID, 3600000, by)

    def test_live_terminal_unknown_and_leased_items_refuse_without_mutation(self):
        original = self.store.get(ID)
        for change in ({'state': s} for s in ('dispatching', 'implementing', 'closed', 'cancelled', 'exhausted', 'merged', 'merge_pending')):
            self.store.put(dict(self.store.get(ID), **change))
            before = self.store.get(ID)
            with self.assertRaises(ToolError):
                self.set_timeout(3600000)
            self.assertEqual(self.store.get(ID), before)
        for marker in ('activeWorkOrderId', 'providerTaskId'):
            self.store.put(dict(original, stateVersion=self.store.get(ID)['stateVersion'],
                                state='blocked', **{marker: 'live'}))
            with self.assertRaises(ToolError):
                self.set_timeout(None)
        self.store.put(dict(original, stateVersion=self.store.get(ID)['stateVersion']))
        self.assertTrue(self.store.acquire(ID, 'someone-else'))
        with self.assertRaisesRegex(ToolError, 'ticked'):
            self.set_timeout(3600000)
        self.store.release(ID, 'someone-else')
        with self.assertRaises(ToolError):
            self.foreman.set_timeout('o/r#404', 3600000, 'owner')

    def test_relay_cap_or_unavailability_refuses_before_the_write(self):
        for cap in (1800000, 3599999):
            self.relay.max_timeout_ms = cap
            before = self.store.get(ID)
            with self.assertRaisesRegex(ToolError, 'relay'):
                self.set_timeout(3600000)
            self.assertEqual(self.store.get(ID), before)
        self.relay.max_timeout_ms = 3600000
        self.set_timeout(3600000)
        self.relay.down = True
        before = self.store.get(ID)
        with self.assertRaisesRegex(ToolError, 'relay'):
            self.set_timeout(None)
        self.assertEqual(self.store.get(ID), before)

    def test_a_reduced_relay_cap_stops_dispatch_without_spending_a_pass(self):
        self.set_timeout(3600000)
        self.relay.max_timeout_ms = 1800000
        wi = self.foreman.tick(ID)['workItem']
        self.assertEqual(wi['state'], 'human_waiting')
        self.assertIn('relay', wi['blockedOn'])
        self.assertEqual(self.store.workorders(ID), [])
        self.assertEqual(self.relay.tasks, {})

    def test_exhausted_pass_budget_still_stops_dispatch(self):
        wi = self.set_timeout(3600000)
        for n in range(self.config.max_passes):
            self.store.save_workorder(documents.workorder(wi, 'implement', n, self.config))
        self.assertEqual(self.foreman.tick(ID)['workItem']['state'], 'exhausted')
        self.assertEqual(self.relay.tasks, {})

    def test_override_survives_restart_and_legacy_store_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'factory.sqlite3')
            store = Store(path)
            store.put(dict(self.set_timeout(3600000), stateVersion=0))
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.get(ID)['wallMsOverride'], 3600000)
            reopened.close()
            db = sqlite3.connect(path)
            legacy = self.store.get(ID)
            legacy.pop('wallMsOverride')
            db.execute('UPDATE workitems SET doc=?', (json.dumps(legacy),))
            db.execute('PRAGMA user_version=7')
            db.commit()
            db.close()
            reopened = Store(path)
            migrated = reopened.get(ID)
            self.assertIsNone(migrated['wallMsOverride'])
            self.assertEqual(documents.workorder(migrated, 'implement', 0, self.config)['budget']['wallMs'], 1800000)
            reopened.close()

    def test_schema_accepts_only_a_bounded_optional_override(self):
        validator = Draft202012Validator(json.loads((ROOT/'schemas/workitem.schema.json').read_text()))
        wi = self.store.get(ID)
        for value, valid in ((None, True), (60000, True), (3600000, True),
                             (59999, False), (3600001, False), (True, False), ('3600000', False)):
            with self.subTest(value=value):
                self.assertEqual(validator.is_valid(dict(wi, wallMsOverride=value)), valid)
        self.assertTrue(validator.is_valid({k: v for k, v in wi.items() if k != 'wallMsOverride'}))
