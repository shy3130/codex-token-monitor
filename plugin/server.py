#!/usr/bin/env python3
"""Read-only local MCP plugin. No third-party packages, network accounts or uploads."""
import base64
import json
import os
from pathlib import Path
import sys
import urllib.request
from urllib.parse import urlsplit, urlencode

ROOT = Path(__file__).resolve().parent
URI = 'ui://token-monitor/panel.html'
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
    'openai/toolInvocation/invoking': '读取本机用量…',
    'openai/toolInvocation/invoked': 'Codex Token Monitor',
}


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


def tool_result():
    try:
        data = stats()
    except Exception:
        return {'isError': True, 'content': [{'type': 'text', 'text':
            '暂时无法连接本机采集服务。请先启动 Codex Token Monitor 并检查本地端口。'}]}
    today = data['today']
    latest = data.get('latest')
    speed = ('最近实测 %.1f token/s。' % latest['tps']) if latest else '等待首个完整请求计时。'
    return {'structuredContent': data, 'content': [{'type': 'text', 'text':
        '今日 %s token（输入 %s，输出 %s）。%s' %
        (format(today['total'], ','), format(today['input'], ','),
         format(today['output'], ','), speed)}]}


def history_result(params):
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
            'text': '已读取 %d 条请求计时记录，范围内共 %d 条。' % (len(data['records']), data['total'])}]}
    except Exception:
        return {'isError': True, 'content': [{'type': 'text', 'text': '暂时无法读取请求记录，请检查采集服务版本或稍后重试。'}]}


def dispatch(method, params):
    if method == 'initialize':
        version = params.get('protocolVersion', '2025-06-18')
        return {'protocolVersion': version,
            'serverInfo': {'name': 'token-monitor', 'version': '1.5.0', 'icons': ICONS},
            'capabilities': {'tools': {}, 'resources': {}}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': [
            {'name': 'open_token_monitor', 'title': 'Codex Token Monitor', 'icons': ICONS,
             'description': '打开本机 Token 与 TPS 监测面板。显示实际采集数据、请求状态和测量时间。',
             'inputSchema': INPUT, 'annotations': ANNOTATIONS, '_meta': UI_META},
            {'name': 'get_token_usage', 'title': '读取本地用量', 'icons': ICONS,
             'description': '读取本机真实 Token 与请求 TPS 数据。面板通过此只读工具刷新，不重新调用模型。',
             'inputSchema': INPUT, 'annotations': ANNOTATIONS,
             '_meta': {'ui': {'visibility': ['model', 'app']}}},
            {'name': 'get_request_history', 'title': '读取请求记录',
             'description': '分页读取所选时间范围的本机请求计时记录；通过返回的 next_cursor 读取后续页。',
             'inputSchema': HISTORY_INPUT, 'annotations': ANNOTATIONS,
             '_meta': {'ui': {'visibility': ['model', 'app']}}}
        ]}
    if method == 'tools/call':
        if params.get('name') == 'get_request_history':
            return history_result(params.get('arguments') or {})
        if params.get('name') not in ('open_token_monitor', 'get_token_usage'):
            raise ValueError('Unknown tool')
        if params.get('arguments') not in (None, {}):
            raise ValueError('This read-only tool does not accept arguments')
        return tool_result()
    if method == 'resources/list':
        return {'resources': [{'uri': URI, 'name': 'Codex Token Monitor',
            'description': '本地 Token 和 TPS 面板', 'mimeType': MIME}]}
    if method == 'resources/templates/list':
        return {'resourceTemplates': []}
    if method == 'resources/read':
        if params.get('uri') != URI:
            raise ValueError('Unknown resource')
        return {'contents': [{'uri': URI, 'mimeType': MIME,
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
