import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('token_monitor_mcp', ROOT / 'plugin/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class MCPProjectTests(unittest.TestCase):
    def test_new_ui_resource_has_its_own_version_and_legacy_uri_still_works(self):
        version = json.loads((ROOT / 'plugin/plugin.json').read_text())['version']
        self.assertEqual(server.URI, f'ui://token-monitor/panel-v{version}.html')
        self.assertNotEqual(server.URI, server.LEGACY_URI)
        current = server.dispatch('resources/read', {'uri': server.URI})['contents'][0]
        legacy = server.dispatch('resources/read', {'uri': server.LEGACY_URI})['contents'][0]
        self.assertEqual(current['text'], legacy['text'])
        self.assertIn('model-kpis', current['text'])
        self.assertEqual(legacy['uri'], server.LEGACY_URI)

    def test_default_and_custom_backend_ports(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server.backend_stats_url(), 'http://127.0.0.1:8765/api/stats')
        with mock.patch.dict(os.environ, {'TOKEN_MONITOR_BACKEND_URL': 'http://localhost:8766/'}):
            self.assertEqual(server.backend_stats_url(), 'http://localhost:8766/api/stats')

    def test_backend_cannot_send_local_data_to_a_remote_or_embedded_url(self):
        bad = ['https://example.com', 'http://example.com:8765',
               'http://127.0.0.1.example.com:8765', 'http://user:pass@localhost:8765',
               'http://localhost:8765/path', 'http://localhost:8765/?token=secret',
               'http://localhost:0', 'http://localhost:99999']
        for value in bad:
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {'TOKEN_MONITOR_BACKEND_URL': value}):
                    with self.assertRaises(ValueError):
                        server.backend_stats_url()

    def test_standard_mcp_tool_and_packaged_ui_are_available(self):
        tools = server.dispatch('tools/list', {})['tools']
        self.assertEqual({t['name'] for t in tools}, {'open_token_monitor', 'get_token_usage', 'get_request_history'})
        opener = next(t for t in tools if t['name'] == 'open_token_monitor')
        self.assertEqual(opener['_meta']['ui']['resourceUri'], server.URI)
        resource = server.dispatch('resources/read', {'uri': server.URI})['contents'][0]
        self.assertEqual(resource['mimeType'], 'text/html;profile=mcp-app')
        self.assertIn('模型使用与费用', resource['text'])

    def test_backend_failure_is_reported_as_a_tool_error(self):
        with mock.patch.object(server, 'stats', side_effect=OSError('unavailable')):
            result = server.dispatch('tools/call', {'name': 'get_token_usage', 'arguments': {}})
        self.assertTrue(result['isError'])
        self.assertIn('采集服务', result['content'][0]['text'])

    def test_history_rejects_invalid_arguments(self):
        for args in ({'limit': True}, {'limit': 101}, {'period': 'all'}, {'extra': 'value'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                server.dispatch('tools/call', {'name': 'get_request_history', 'arguments': args})


if __name__ == '__main__':
    unittest.main()
