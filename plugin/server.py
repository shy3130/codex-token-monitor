#!/usr/bin/env python3
"""Read-only local MCP plugin. No third-party packages, network accounts or uploads."""
import base64
import json
import os
from pathlib import Path
import re
import sys
import urllib.request
from urllib.parse import urlsplit, urlencode

ROOT = Path(__file__).resolve().parent
VERSION = json.loads((ROOT / 'plugin.json').read_text())['version']
URI = f'ui://token-monitor/panel-v{VERSION}.html'
LEGACY_URI = 'ui://token-monitor/panel.html'
MIME = 'text/html;profile=mcp-app'
ICONS = [{'src': 'data:image/png;base64,' + base64.b64encode((ROOT / 'assets/icon.png').read_bytes()).decode('ascii'),
          'mimeType': 'image/png', 'sizes': ['256x256']}]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
INPUT = {'type': 'object', 'properties': {}, 'additionalProperties': False}
HISTORY_INPUT = {'type': 'object', 'properties': {
    'period': {'type': 'string', 'enum': ['today', '7d', '30d']},
    'cursor': {'type': 'string', 'maxLength': 512},
    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, 'additionalProperties': False}
ANNOTATIONS = {'readOnlyHint': True, 'destructiveHint': False,
               'idempotentHint': True, 'openWorldHint': False}
UI_META = {
    'ui': {'resourceUri': URI, 'visibility': ['model', 'app']},
    'openai/outputTemplate': URI,
    'openai/widgetAccessible': True,
    'openai/ui': {'entrypoints': [{'type': 'thread'}, {'type': 'global'}]},
    'openai/toolInvocation/invoking': 'Reading local usage…',
    'openai/toolInvocation/invoked': 'Codex Token Monitor',
}
CLIENT_LOCALE = ''
MESSAGES = {
    'connecting': ('读取本机用量…', 'Reading local usage…'),
    'backend_error': ('暂时无法连接本机采集服务。请先启动 Codex Token Monitor 并检查本地端口。',
                      'Unable to connect to the local collector. Start Codex Token Monitor and check its local port.'),
    'latest': ('最近实测 %.1f token/s。', 'Latest measured speed: %.1f token/s.'),
    'waiting': ('等待首个完整请求计时。', 'Waiting for the first complete request timing.'),
    'summary': ('今日 %s token（输入 %s，输出 %s）。%s', 'Today: %s tokens (%s input, %s output). %s'),
    'history': ('已读取 %d 条请求计时记录，范围内共 %d 条。', 'Loaded %d request timings; %d records in this range.'),
    'history_error': ('暂时无法读取请求记录，请检查采集服务版本或稍后重试。',
                      'Unable to read request history. Check the collector version or try again shortly.'),
    'open_description': ('打开本机 Token 与 TPS 监测面板。显示实际采集数据、请求状态和测量时间。',
                         'Open the local token and TPS dashboard with collected usage, request status and measurement times.'),
    'usage_title': ('读取本地用量', 'Read local usage'),
    'usage_description': ('读取本机真实 Token 与请求 TPS 数据。面板通过此只读工具刷新，不重新调用模型。',
                          'Read local token usage and request TPS. This read-only tool refreshes the dashboard without calling a model.'),
    'history_title': ('读取请求记录', 'Read request history'),
    'history_description': ('分页读取所选时间范围的本机请求计时记录；通过返回的 next_cursor 读取后续页。',
                            'Read local request timings for a selected range; use next_cursor for subsequent pages.'),
    'resource_description': ('本地 Token 和 TPS 面板', 'Local token usage and TPS dashboard'),
}


def locale_hint(params):
    """Locale metadata is an optional host hint; never infer it from message text."""
    meta = params.get('_meta')
    if not isinstance(meta, dict):
        return ''
    for key in ('openai/locale', 'webplus/i18n'):
        value = meta.get(key)
        if isinstance(value, str) and re.fullmatch(r'[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*', value):
            return value.replace('_', '-')
    return ''


def text(key, locale):
    return MESSAGES[key][0 if locale.lower().split('-')[0] == 'zh' else 1]


def backend_stats_url():
    """Allow alternate local ports, but never transmit usage to a remote host."""
    value = os.environ.get('TOKEN_MONITOR_BACKEND_URL', 'http://127.0.0.1:8765')
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError('Invalid local backend port') from error
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost')
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or port == 0):
        raise ValueError('Backend must be a loopback HTTP origin')
    return value.rstrip('/') + '/api/stats'


def stats():
    with OPENER.open(backend_stats_url(), timeout=4) as response:
        data = json.load(response)
    data['speeds'] = data.get('speeds', [])[:24]
    data['sessions'] = data.get('sessions', [])[:8]
    return data


def tool_result(locale='en'):
    try:
        data = stats()
    except Exception:
        return {'isError': True, 'content': [{'type': 'text', 'text':
            text('backend_error', locale)}]}
    today = data['today']
    latest = data.get('latest')
    speed = (text('latest', locale) % latest['tps']) if latest else text('waiting', locale)
    return {'structuredContent': data, 'content': [{'type': 'text', 'text':
        text('summary', locale) %
        (format(today['total'], ','), format(today['input'], ','),
         format(today['output'], ','), speed)}]}


def history_result(params, locale='en'):
    if (not isinstance(params, dict) or set(params)-{'period', 'cursor', 'limit'}
            or params.get('period', 'today') not in ('today', '7d', '30d')
            or type(params.get('limit', 20)) is not int or not 1 <= params.get('limit', 20) <= 100
            or ('cursor' in params and (not isinstance(params['cursor'], str) or len(params['cursor']) > 512))):
        raise ValueError('Invalid history arguments')
    try:
        url = backend_stats_url().rsplit('/', 1)[0] + '/requests?' + urlencode(params)
        with OPENER.open(url, timeout=4) as response:
            data = json.load(response)
        return {'structuredContent': data, 'content': [{'type': 'text',
            'text': text('history', locale) % (len(data['records']), data['total'])}]}
    except Exception:
        return {'isError': True, 'content': [{'type': 'text', 'text': text('history_error', locale)}]}


def dispatch(method, params):
    global CLIENT_LOCALE
    hint = locale_hint(params)
    locale = hint or CLIENT_LOCALE or 'en'
    if method == 'initialize':
        CLIENT_LOCALE = hint
        version = params.get('protocolVersion', '2025-06-18')
        return {'protocolVersion': version,
            'serverInfo': {'name': 'token-monitor', 'version': VERSION, 'icons': ICONS},
            'capabilities': {'tools': {}, 'resources': {}}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [
            {'name': 'open_token_monitor', 'title': 'Codex Token Monitor', 'icons': ICONS,
             'description': text('open_description', locale),
             'inputSchema': INPUT, 'annotations': ANNOTATIONS,
             '_meta': {**UI_META, 'openai/toolInvocation/invoking': text('connecting', locale)}},
            {'name': 'get_token_usage', 'title': text('usage_title', locale), 'icons': ICONS,
             'description': text('usage_description', locale),
             'inputSchema': INPUT, 'annotations': ANNOTATIONS,
             '_meta': {'ui': {'visibility': ['model', 'app']}}},
            {'name': 'get_request_history', 'title': text('history_title', locale),
             'description': text('history_description', locale),
             'inputSchema': HISTORY_INPUT, 'annotations': ANNOTATIONS,
             '_meta': {'ui': {'visibility': ['model', 'app']}}}
        ]}
    if method == 'tools/call':
        if params.get('name') == 'get_request_history':
            result = history_result(params.get('arguments') or {}, locale)
        else:
            if params.get('name') not in ('open_token_monitor', 'get_token_usage'):
                raise ValueError('Unknown tool')
            if params.get('arguments') not in (None, {}):
                raise ValueError('This read-only tool does not accept arguments')
            result = tool_result(locale)
        # Forward only an actual host hint. A default must not override the browser language.
        if hint or CLIENT_LOCALE:
            result['_meta'] = {'token-monitor/locale': hint or CLIENT_LOCALE}
        return result
    if method == 'resources/list':
        return {'resources': [{'uri': URI, 'name': 'Codex Token Monitor',
            'description': text('resource_description', locale), 'mimeType': MIME}]}
    if method == 'resources/templates/list':
        return {'resourceTemplates': []}
    if method == 'resources/read':
        requested = params.get('uri')
        if requested not in (URI, LEGACY_URI):
            raise ValueError('Unknown resource')
        return {'contents': [{'uri': requested, 'mimeType': MIME,
            'text': (ROOT / 'ui/monitor.html').read_text(),
            '_meta': {'ui': {'prefersBorder': False,
                'csp': {'connectDomains': [], 'resourceDomains': []}}}}]}
    raise LookupError('Method not found')


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            response = {'jsonrpc': '2.0', 'id': None,
                        'error': {'code': -32700, 'message': 'Parse error'}}
        else:
            if not isinstance(request, dict):
                response = {'jsonrpc': '2.0', 'id': None,
                            'error': {'code': -32600, 'message': 'Invalid request'}}
            elif 'id' not in request:
                continue
            else:
                response = {'jsonrpc': '2.0', 'id': request['id']}
                try:
                    response['result'] = dispatch(request.get('method'), request.get('params') or {})
                except ValueError as error:
                    response['error'] = {'code': -32602, 'message': str(error)}
                except LookupError:
                    response['error'] = {'code': -32601, 'message': 'Method not found'}
                except Exception:
                    response['error'] = {'code': -32603, 'message': 'Local plugin error'}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
