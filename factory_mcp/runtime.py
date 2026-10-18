"""Sanitized operational facts. No model calls, host writes, or credential contents."""
import hashlib
import http.client
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

from .github import GitHubError
from .mcp import ToolError
from .relay import RelayError
from .verdict import verdicts

ROOT = Path(__file__).resolve().parent.parent


def _read(argv):
    try:
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip() if result.returncode == 0 else ''
    pattern = r'[0-9a-f]{40}' if argv[0] == 'git' else r'sha256:[0-9a-f]{64}'
    return value if re.fullmatch(pattern, value) else None


def _arg(args, flag, default=None):
    if flag in args and args.index(flag) + 1 < len(args):
        return args[args.index(flag) + 1]
    return default


def _factory_runner(command, args):
    """Only label the standard Python runner invocation as sandboxed."""
    if (command != 'python3' or not isinstance(args, list) or not args
            or not all(isinstance(arg, str) for arg in args)):
        return False
    script = Path(args[0])
    return (script.is_absolute() and '..' not in script.parts and
            script.parts[-2:] == ('worker', 'runner.py') and
            '--' in args and args.index('--') < len(args) - 1)


def _credential_paths(args):
    paths = []
    for index, arg in enumerate(args):
        if arg == '--credential':
            if index + 1 >= len(args):
                paths.append(None)
                continue
            host, separator, target = args[index + 1].rpartition(':')
            paths.append(os.path.expanduser(host) if separator and host and target.startswith('/') else None)
    return paths


def _discovery(schemas):
    expected = {tool['name']: tool.get('inputSchema', {}).get('properties', {}) for tool in schemas}
    result = {'serverSchemaHash': hashlib.sha256(json.dumps(schemas, sort_keys=True).encode()).hexdigest(),
              'cacheObservationOnly': True, 'missingFromCache': [], 'missingCachedFields': {},
              'refreshAdvice': 'If this chat has old tools, use /reload-mcp in that chat. A disk cache is not proof of its active schema.'}
    path = Path.home()/'.hermes/profiles/day-desk/cache/mcp_schema_cache.json'
    try:
        cached = json.loads(path.read_text())['factory_drive']['tools']
        cached = {row['name']: row.get('inputSchema', {}).get('properties', {}) for row in cached}
    except (OSError, ValueError, KeyError, TypeError):
        result['cacheStatus'] = 'unavailable'
        return result
    result['cacheStatus'] = 'readable'
    result['missingFromCache'] = sorted(set(expected) - set(cached))
    result['missingCachedFields'] = {name: sorted(set(fields) - set(cached[name]))
                                     for name, fields in expected.items()
                                     if name in cached and set(fields) - set(cached[name])}
    return result


def _github(foreman, wi):
    github, repo, number = foreman.github, wi['repo'], wi.get('prNumber')
    result = {'repo': repo, 'prNumber': number, 'storedHead': wi.get('headSha'), 'liveHead': None,
              'credentialSource': 'factory GitHub client'}
    if not number:
        return result
    try:
        result['liveHead'] = github.pull(repo, number)['head']['sha']
    except (GitHubError, OSError, http.client.HTTPException) as exc:
        result['pullStatus'] = 'forbidden' if getattr(exc, 'status', None) == 403 else 'unavailable'
        return result
    head = result['liveHead']
    for name, read in [('comments', lambda: github.comments(repo, number)),
                       ('checks', lambda: github.check_runs(repo, head)),
                       ('workflowRuns', lambda: github.workflow_runs(repo, head))]:
        try:
            rows = read()
            result[name] = {'status': 'ok', 'rows': len(rows)}
            if name == 'comments':
                current = [v for v in verdicts(rows, repo, number) if v['headSha'] == head]
                result['headVerdict'] = ({key: current[-1][key] for key in ('round', 'outcome')}
                                         if current else None)
        except (GitHubError, OSError, http.client.HTTPException) as exc:
            result[name] = {'status': 'forbidden' if getattr(exc, 'status', None) == 403 else 'unavailable'}
    return result


def report(foreman, schemas, workitem_id=None):
    if workitem_id is not None and (not isinstance(workitem_id, str) or not workitem_id):
        raise ToolError('id must name a known WorkItem')
    wi = foreman.store.get(workitem_id) if workitem_id else None
    if workitem_id and wi is None:
        raise ToolError('No known WorkItem for this id')
    config = foreman.config
    result = {'checkedAtIST': datetime.now(timezone(timedelta(hours=5, minutes=30))).isoformat(),
              'revision': _read(['git', 'rev-parse', 'HEAD']), 'factoryResponding': True,
              'globalWallMs': config.wall_ms, 'workerOrder': [name for name, _ in config.workers],
              'workers': [], 'discovery': _discovery(schemas)}
    try:
        rows = json.loads(Path(config.agents_file).read_text())['agents']
        agents = {row['id']: row for row in rows}
        result['registryStatus'] = 'readable'
    except (OSError, ValueError, KeyError, TypeError):
        agents, result['registryStatus'] = {}, 'unavailable'
    images = {}
    registered = (name for name, agent in agents.items()
                  if _factory_runner(agent.get('command'), agent.get('args')))
    names = list(dict.fromkeys([*result['workerOrder'], *registered]))
    for name in names:
        agent = agents.get(name)
        if agent is None:
            result['workers'].append({'id': name, 'status': 'not_registered'})
            continue
        args = agent.get('args')
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            result['workers'].append({'id': name, 'status': 'invalid_args',
                                      'allowed': name in result['workerOrder'], 'sandboxed': None,
                                      'agentDefaultTimeoutMs': agent.get('timeoutMs')})
            continue
        sandboxed = _factory_runner(agent.get('command'), args)
        worker = {'id': name, 'status': 'registered', 'allowed': name in result['workerOrder'],
                  'sandboxed': sandboxed, 'agentDefaultTimeoutMs': agent.get('timeoutMs')}
        if sandboxed:
            image = _arg(args, '--image', 'factory-worker:latest')
            if image not in images:
                images[image] = _read(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
            credentials = _credential_paths(args)
            worker.update({'model': _arg(args, '--models', _arg(args, '--model')),
                           'image': image, 'imageId': images[image],
                           'credentialFilesReadable': bool(credentials) and all(
                               path is not None and Path(path).is_file() and os.access(path, os.R_OK)
                               for path in credentials),
                           'providerAuthentication': 'not_probed'})
        result['workers'].append(worker)
    try:
        result['relay'] = {'status': 'ok', 'maxTimeoutMs': foreman.relay.limits()['maxTimeoutMs']}
    except RelayError:
        result['relay'] = {'status': 'unavailable'}
    if wi:
        result['item'] = {'id': wi['id'], 'state': wi['state'], 'worker': wi.get('worker'),
                          'wallMsOverride': wi.get('wallMsOverride'),
                          'effectiveWallMs': (wi.get('wallMsOverride')
                                              if wi.get('wallMsOverride') is not None
                                              else config.wall_ms)}
        result['github'] = _github(foreman, wi)
    return result
