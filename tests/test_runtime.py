"""Runtime diagnostics expose operational facts, never the credential or environment values."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.store import Store
from factory_mcp import tools
from tests.fakes import FakeGitHub, FakeRelay, workitem


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.auth = self.home/'auth.json'
        self.auth.write_text('{"key":"CANARY-CREDENTIAL-VALUE"}')
        self.agents = self.home/'agents.json'
        self.agent = {'id':'opencode-mimo-factory','command':'python3', 'args':[
            '/deployed/worker/runner.py','--credential',str(self.auth)+':/tmp/factory-home/auth',
            '--env','PRIVATE_VALUE=CANARY-ENV-VALUE','--','opencode','run','--model',
            'opencode/mimo-v2.6-flash-free'], 'timeoutMs':1500000}
        self.agents.write_text(json.dumps({'agents':[self.agent]}))
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.config = Config(mcp_token='CANARY-OWNER-TOKEN',github_token='CANARY-GITHUB-TOKEN',
                             repos=frozenset({'o/r'}),agents_file=str(self.agents),wall_ms=1800000,
                             workers=(('opencode-mimo-factory','local'),))
        self.foreman = Foreman(self.store,self.github,self.config,self.relay)
        self.store.put(workitem(state='blocked',wallMsOverride=3600000,prNumber=9,headSha='stored'))
        self.github.add_pr('o/r',9,'factory/1','live')
        self.github.add_run('o/r','live')

    def report(self):
        from factory_mcp import runtime
        def run(argv, **kwargs):
            if argv[0]=='git':return subprocess.CompletedProcess(argv,0,'a'*40+'\n','')
            return subprocess.CompletedProcess(argv,0,'sha256:'+'b'*64+'\n','')
        with mock.patch.object(runtime.Path,'home',return_value=self.home), \
             mock.patch.object(runtime.subprocess,'run',side_effect=run):
            return tools.build(self.store,self.foreman,agent=True)['factory_runtime_status'].fn({'id':'o/r#1'})

    def test_effective_budget_worker_model_image_and_readability_are_visible_without_secrets(self):
        before=self.store.get('o/r#1')
        report=self.report()
        self.assertEqual(report['revision'],'a'*40)
        self.assertEqual(report['workerOrder'],['opencode-mimo-factory'])
        worker=report['workers'][0]
        self.assertEqual((worker['model'],worker['imageId']),
                         ('opencode/mimo-v2.6-flash-free','sha256:'+'b'*64))
        self.assertTrue(worker['credentialFilesReadable'])
        self.assertEqual(report['item']['effectiveWallMs'],3600000)
        self.assertEqual(report['globalWallMs'],1800000)
        self.assertEqual((report['github']['storedHead'],report['github']['liveHead']),('stored','live'))
        for name in ('comments','checks','workflowRuns'):
            self.assertEqual(report['github'][name]['status'],'ok')
        rendered=json.dumps(report)
        for secret in ('CANARY-CREDENTIAL-VALUE','CANARY-ENV-VALUE','CANARY-OWNER-TOKEN','CANARY-GITHUB-TOKEN'):
            self.assertNotIn(secret,rendered)
        self.assertEqual(self.store.get('o/r#1'),before)
        self.assertEqual(self.relay.tasks,{})
        self.assertEqual(self.store.workorders('o/r#1'),[])

    def test_missing_credentials_and_registry_do_not_look_healthy(self):
        self.auth.unlink()
        self.assertFalse(self.report()['workers'][0]['credentialFilesReadable'])
        self.agents.unlink()
        report=self.report()
        self.assertEqual(report['registryStatus'],'unavailable')
        self.assertEqual(report['workers'][0]['status'],'not_registered')

    def test_old_cached_schema_is_reported_as_cache_observation_with_refresh_advice(self):
        cache=self.home/'.hermes/profiles/day-desk/cache/mcp_schema_cache.json'
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({'factory_drive':{'tools':[{
            'name':'factory_register_worker','inputSchema':{'properties':{'worker':{},'model':{},'by':{}}}}]}}))
        cache=self.report()['discovery']
        self.assertIn('factory_set_timeout',cache['missingFromCache'])
        self.assertIn('template',cache['missingCachedFields']['factory_register_worker'])
        self.assertTrue(cache['cacheObservationOnly'])
        self.assertIn('/reload-mcp',cache['refreshAdvice'])

    def test_read_denials_are_statuses_not_sensitive_api_errors(self):
        from factory_mcp.github import GitHubError
        def denied(*args,**kwargs):raise GitHubError(403,'CANARY-GITHUB-TOKEN')
        self.github.check_runs=denied
        report=self.report()
        self.assertEqual(report['github']['checks']['status'],'forbidden')
        self.assertNotIn('CANARY-GITHUB-TOKEN',json.dumps(report))

    def test_discovery_hash_uses_the_tool_set_for_the_current_token(self):
        from factory_mcp import runtime
        seen = []
        with mock.patch.object(runtime, 'report', side_effect=lambda foreman, schemas, item_id:
                               seen.append({tool['name'] for tool in schemas}) or {}):
            tools.build(self.store, self.foreman)['factory_runtime_status'].fn({})
            owner_tools = seen.pop()
            tools.build(self.store, self.foreman, agent=True)['factory_runtime_status'].fn({})
            agent_tools = seen.pop()
        self.assertIn('factory_bootstrap_repo', owner_tools)
        self.assertNotIn('factory_bootstrap_repo', agent_tools)
        self.assertIn('factory_runtime_status', agent_tools)
