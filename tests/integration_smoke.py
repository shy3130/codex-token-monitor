#!/usr/bin/env python3
"""Isolated end-to-end smoke test. Uses synthetic records and a temporary port."""
import datetime as dt
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def event(name, stamp, **values):
    attrs = {'event.name': name, 'event.timestamp': stamp, 'conversation.id': 'smoke-thread',
             'model': 'gpt-6.1-sol', **values}
    return {'attributes': [{'key': k, 'value': {'stringValue': str(v)}} for k, v in attrs.items()]}


def iso(stamp):
    return stamp.isoformat().replace('+00:00', 'Z')


def main():
    with tempfile.TemporaryDirectory(prefix='token-monitor-smoke-') as tmp:
        root = Path(tmp)
        sessions = root / 'codex' / 'sessions'
        sessions.mkdir(parents=True)
        stamp = dt.datetime.now(dt.timezone.utc)
        usage = {'input_tokens': 1000, 'cached_input_tokens': 800,
                 'output_tokens': 100, 'reasoning_output_tokens': 20}
        rows = [
            {'type': 'session_meta', 'payload': {'id': 'smoke-thread'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'turn-1', 'model': 'gpt-6.1-sol', 'effort': 'xhigh'}},
            {'type': 'token_usage_record', 'timestamp': iso(stamp),
             'payload': {'response_id': 'response-1', 'turn_id': 'turn-1', 'usage': usage}},
        ]
        (sessions / 'rollout.jsonl').write_text('\n'.join(map(json.dumps, rows)) + '\n')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        process = subprocess.Popen([sys.executable, str(ROOT / 'monitor.py'), '--port', str(port),
                                    '--codex-dir', str(root / 'codex'), '--data-dir', str(root / 'data')],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(50):
                if process.poll() is not None:
                    raise RuntimeError('Local collector exited before starting')
                try:
                    with opener.open(base + '/health', timeout=1) as response:
                        assert json.load(response)['ok']
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise TimeoutError('Local collector did not start')
            records = [
                event('codex.websocket_request', iso(stamp - dt.timedelta(seconds=2)), success='true'),
                event('codex.sse_event', iso(stamp), **{'event.kind': 'response.completed',
                    'input_token_count': 1000, 'cached_token_count': 800, 'output_token_count': 100,
                    'reasoning_token_count': 20}),
            ]
            payload = {'resourceLogs': [{'scopeLogs': [{'logRecords': records}]}]}
            request = urllib.request.Request(base + '/v1/logs',
                data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
            with opener.open(request, timeout=2) as response:
                assert response.status == 200
            with opener.open(base + '/api/stats', timeout=2) as response:
                data = json.load(response)
            assert data['today']['requests'] == 1 and data['today']['total'] == 1100
            assert abs(data['latest']['tps'] - 50) < .01
            assert data['latest']['effort'] == 'xhigh'
            with opener.open(base + '/api/requests?period=today&limit=10', timeout=2) as response:
                history = json.load(response)
            assert history['total'] == 1 and history['records'][0]['effort'] == 'xhigh'
            assert history['next_cursor'] is None
            with opener.open(base + '/', timeout=2) as response:
                assert '模型使用与费用' in response.read().decode()
            mcp_requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                 'params': {'name': 'get_token_usage', 'arguments': {}}},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'resources/read',
                 'params': {'uri': 'ui://token-monitor/panel.html'}},
                {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
                 'params': {'name': 'get_request_history', 'arguments': {'period': 'today', 'limit': 10}}},
            ]
            env['TOKEN_MONITOR_BACKEND_URL'] = base
            raw = subprocess.check_output([sys.executable, str(ROOT / 'plugin/server.py')],
                input=('\n'.join(map(json.dumps, mcp_requests)) + '\n').encode(), env=env, timeout=5)
            replies = {item['id']: item['result'] for item in map(json.loads, raw.splitlines())}
            assert replies[2]['structuredContent']['today']['total'] == 1100
            assert replies[3]['contents'][0]['mimeType'] == 'text/html;profile=mcp-app'
            assert replies[4]['structuredContent']['records'][0]['tps'] == 50
            print('OK: standalone collector, synthetic telemetry, UI and stdio MCP client')
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            if process.stderr:
                process.stderr.close()


if __name__ == '__main__':
    main()
