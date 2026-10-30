#!/usr/bin/env python3
"""Loopback-only Codex token accounting and OTLP request throughput monitor.

Uses Python's standard library. Stores numeric usage, model names and identifiers,
never prompts, tool arguments, account names or model response contents.
"""
import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
VERSION = '1.5.0'
TZ = dt.timezone(dt.timedelta(hours=8))


def epoch(value):
    try:
        stamp = (float(value) if isinstance(value, (int, float)) else
                 dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
        return stamp if math.isfinite(stamp) and 0 < stamp < 253402300800 else 0.0
    except (TypeError, ValueError, AttributeError, OverflowError, OSError):
        return 0.0


def nanoseconds_epoch(value):
    # OTLP nanoseconds are ~1e18 today, beyond the token-count safety limit.
    try:
        return epoch(int(value) / 1_000_000_000)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def number(value):
    try:
        n = float(value)
        return n if n >= 0 and n < 1e16 else 0
    except (ValueError, TypeError, OverflowError):
        return 0


EFFORTS = frozenset(('none', 'minimal', 'low', 'medium', 'high', 'xhigh',
                     'max', 'ultra', 'persistent'))
# Standard text-token API prices per million, verified on 2026-10-29.
# Rates are (fresh input, cached input, output, vendor). These hypothetical
# rates are not subscription spending or remaining account credit.
API_PRICE_DATE = '2026-10-29'
API_PRICES = {
    'gpt-6-astra': (10.0, 1.0, 50.0, 'OpenAI'),
    'gpt-6.1-sol': (2.0, 0.10, 10.0, 'OpenAI'),
    'gpt-6.1-terra': (1.0, 0.05, 5.0, 'OpenAI'),
    'gpt-6-sol': (2.0, 0.20, 10.0, 'OpenAI'),
    'gpt-6-luna': (0.10, 0.01, 0.50, 'OpenAI'),
    'gpt-5.6': (4.0, 0.40, 20.0, 'OpenAI'),  # Official alias for GPT-5.6 Sol.
    'gpt-5.6-sol': (4.0, 0.40, 20.0, 'OpenAI'),
    'gpt-5.6-terra': (2.0, 0.20, 12.0, 'OpenAI'),
    'gpt-5.6-luna': (0.20, 0.02, 1.20, 'OpenAI'),
    'glm-5.3': (1.40, 0.26, 4.40, 'Z.ai'),
}


def effort_value(value):
    """Keep only recognized reasoning settings, never arbitrary session text."""
    return value if isinstance(value, str) and value in EFFORTS else None


def attributes(items):
    result = {}
    for item in items or []:
        value = item.get('value') or {}
        for kind in ('stringValue', 'intValue', 'doubleValue', 'boolValue'):
            if kind in value:
                result[item.get('key', '')] = value[kind]
                break
    return result


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS usage (
          id TEXT PRIMARY KEY, thread TEXT, turn TEXT, ts REAL, model TEXT,
          input INTEGER, cached INTEGER, output INTEGER, reasoning INTEGER);
        CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
        CREATE TABLE IF NOT EXISTS speed (
          id TEXT PRIMARY KEY, thread TEXT, ts REAL, model TEXT,
          input INTEGER, cached INTEGER, output INTEGER, reasoning INTEGER,
          duration_ms REAL, ttft_ms REAL);
        CREATE INDEX IF NOT EXISTS speed_ts ON speed(ts);
        CREATE TABLE IF NOT EXISTS request_starts (
          id TEXT PRIMARY KEY, thread TEXT, ts REAL, started REAL, model TEXT);
        CREATE INDEX IF NOT EXISTS request_starts_thread ON request_starts(thread,ts);
        CREATE TABLE IF NOT EXISTS request_ends (id TEXT PRIMARY KEY, thread TEXT, ts REAL);
        CREATE INDEX IF NOT EXISTS request_ends_thread ON request_ends(thread,ts);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(speed)')}
        if 'request_start_id' not in columns:
            self.db.execute('ALTER TABLE speed ADD COLUMN request_start_id TEXT')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(usage)')}
        if 'effort' not in columns:
            self.db.execute('ALTER TABLE usage ADD COLUMN effort TEXT')
        self.db.execute('CREATE INDEX IF NOT EXISTS usage_thread_ts ON usage(thread, ts)')
        self.db.execute('CREATE INDEX IF NOT EXISTS speed_thread_ts ON speed(thread, ts)')
        self.db.commit()
        self.last_scan = 0
        self.scan_error = None
        self.files = 0

    def add_usage(self, key, thread, turn, ts, model, usage, effort=None):
        with self.lock:
            self.db.execute('''INSERT INTO usage
                (id,thread,turn,ts,model,input,cached,output,reasoning,effort)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET effort=excluded.effort
                WHERE usage.effort IS NULL AND excluded.effort IS NOT NULL''',
                (key, thread, turn, ts, model, int(number(usage.get('input_tokens'))),
                 int(number(usage.get('cached_input_tokens'))),
                 int(number(usage.get('output_tokens'))),
                 int(number(usage.get('reasoning_output_tokens'))), effort_value(effort)))

    def ingest(self, payload):
        records = []
        for resource in payload.get('resourceLogs', []):
            for scope in resource.get('scopeLogs', []):
                for record in scope.get('logRecords', []):
                    a = attributes(record.get('attributes'))
                    stamp = epoch(a.get('event.timestamp'))
                    if not stamp:
                        stamp = nanoseconds_epoch(record.get('timeUnixNano'))
                    if not stamp:
                        stamp = nanoseconds_epoch(record.get('observedTimeUnixNano'))
                    if stamp:
                        records.append((stamp, a))
        with self.lock:
            affected = {}
            def changed(thread, stamp):
                affected[thread] = min(affected.get(thread, stamp), stamp)
            for stamp, a in sorted(records, key=lambda r: r[0]):
                name = a.get('event.name', '')
                thread = a.get('conversation.id', '')
                if not thread:
                    continue
                if name in ('codex.websocket_request', 'codex.api_request'):
                    if str(a.get('success', '')).lower() != 'true':
                        continue
                    key = hashlib.sha256(('%s|%s|%.6f' %
                        (thread, name, stamp)).encode()).hexdigest()
                    inserted = self.db.execute('INSERT OR IGNORE INTO request_starts VALUES (?,?,?,?,?)',
                        (key, thread, stamp, stamp - number(a.get('duration_ms')) / 1000,
                         a.get('model', 'unknown'))).rowcount
                    if inserted:
                        changed(thread, stamp)
                if name not in ('codex.sse_event', 'codex.websocket_event'):
                    continue
                if a.get('event.kind') != 'response.completed':
                    continue
                inp = int(number(a.get('input_token_count')))
                out = int(number(a.get('output_token_count')))
                key = hashlib.sha256(('%s|%.6f|%d|%d' %
                    (thread, stamp, inp, out)).encode()).hexdigest()
                inserted = self.db.execute('INSERT OR IGNORE INTO request_ends VALUES (?,?,?)',
                                           (key, thread, stamp)).rowcount
                if inserted:
                    changed(thread, stamp)
                if self.db.execute('SELECT 1 FROM speed WHERE id=?', (key,)).fetchone():
                    continue
                if not out:  # Connection warm-up, not a generated answer.
                    continue
                ttft = number(a.get('ttft_ms')) or None
                self.db.execute('''INSERT INTO speed
                    (id,thread,ts,model,input,cached,output,reasoning,duration_ms,ttft_ms)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (key, thread, stamp, a.get('model', 'unknown'), inp,
                     int(number(a.get('cached_token_count'))), out,
                     int(number(a.get('reasoning_token_count'))), None, ttft))
                changed(thread, stamp)
            self.reconcile_timings(affected)
            if records:
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                                ('last_otel_received', str(time.time())))
            self.db.commit()

    def reconcile_timings(self, affected=None):
        """Match durable starts to completions, including reversed OTLP delivery.

        Completion timestamps bound each request, so a missing start cannot reuse
        the previous request or accidentally include time spent running tools.
        """
        # A late completion can invalidate a previously assigned start. Revisit
        # the affected interval, including measured rows, instead of only NULLs.
        if affected is None:
            affected = {r[0]: 0 for r in self.db.execute('SELECT DISTINCT thread FROM speed')}
        for thread, since in affected.items():
            rows = self.db.execute('''SELECT id,ts,duration_ms,request_start_id FROM speed
                WHERE thread=? AND ts>=? ORDER BY ts,id''', (thread, since)).fetchall()
            used_starts = set()
            for row in rows:
                previous = self.db.execute('''SELECT MAX(ts) FROM speed
                    WHERE thread=? AND ts<?''', (thread, row['ts'])).fetchone()[0]
                previous_end = self.db.execute('''SELECT MAX(ts) FROM request_ends
                    WHERE thread=? AND ts<?''', (thread, row['ts'])).fetchone()[0]
                previous = max(previous or 0, previous_end or 0)
                start = self.db.execute('''SELECT id,started FROM request_starts
                    WHERE thread=? AND ts<=? AND started>? AND started>=?
                    ORDER BY ts DESC LIMIT 1''',
                    (thread, row['ts'], previous, row['ts'] - 86400)).fetchone()
                duration = (row['ts'] - start['started']) * 1000 if start else None
                start_id = start['id'] if start and duration > 0 and start['id'] not in used_starts else None
                duration = duration if start_id else None
                if start_id:
                    used_starts.add(start_id)
                if (duration, start_id) != (row['duration_ms'], row['request_start_id']):
                    self.db.execute('UPDATE speed SET duration_ms=?, request_start_id=? WHERE id=?',
                                    (duration, start_id, row['id']))

    def period_summary(self, key, label, start, end, step):
        since, until = start.timestamp(), end.timestamp()
        summary = dict(self.db.execute('''SELECT COUNT(*) requests,
            COUNT(DISTINCT thread) sessions, COALESCE(SUM(input),0) input,
            COALESCE(SUM(cached),0) cached, COALESCE(SUM(output),0) output,
            COALESCE(SUM(reasoning),0) reasoning FROM usage WHERE ts>=? AND ts<?''',
            (since, until)).fetchone())
        summary['total'] = summary['input'] + summary['output']
        summary['fresh'] = max(0, summary['input'] - summary['cached'])
        summary['cache_hit_rate'] = summary['cached'] / summary['input'] if summary['input'] else 0
        speed = dict(self.db.execute('''SELECT COUNT(*) samples,
            COALESCE(SUM(output),0) tokens, COALESCE(SUM(duration_ms),0) ms
            FROM speed WHERE ts>=? AND ts<? AND duration_ms>0''', (since, until)).fetchone())
        speed['tps'] = speed['tokens'] * 1000 / speed['ms'] if speed['ms'] else None
        buckets = {r['bucket']: dict(r) for r in self.db.execute('''
            SELECT CAST((ts-?)/? AS INTEGER) bucket, COUNT(*) requests,
            SUM(input) input, SUM(cached) cached, SUM(output) output,
            SUM(reasoning) reasoning FROM usage WHERE ts>=? AND ts<? GROUP BY bucket''',
            (since, step, since, until))}
        timeline = []
        for index in range(max(1, int((until - since) // step) + 1)):
            stamp = since + index * step
            if stamp >= until:
                break
            point = buckets.get(index, dict(requests=0, input=0, cached=0, output=0, reasoning=0)).copy()
            point.pop('bucket', None)
            point.update(ts=stamp, total=point['input'] + point['output'],
                fresh=max(0, point['input'] - point['cached']),
                label=dt.datetime.fromtimestamp(stamp, TZ).strftime('%H:%M' if step == 3600 else '%m-%d'))
            timeline.append(point)
        models = [dict(r) for r in self.db.execute('''SELECT model, COUNT(*) requests,
            SUM(input) input, SUM(cached) cached, SUM(output) output,
            SUM(reasoning) reasoning, SUM(input)+SUM(output) total FROM usage
            WHERE ts>=? AND ts<? GROUP BY model ORDER BY total DESC''', (since, until))]
        priced, unpriced, dollars = 0, 0, 0.0
        model_costs, model_long = {}, {}
        for r in self.db.execute('''SELECT model, input>272000 long_context,
            COUNT(*) requests, SUM(MAX(input-cached,0)) fresh,
            SUM(MIN(input,cached)) cached, SUM(output) output FROM usage
            WHERE ts>=? AND ts<? GROUP BY model,long_context''', (since, until)):
            rates = API_PRICES.get(r['model'])
            if rates is None:
                unpriced += r['requests']
                model_costs[r['model']] = None
                continue
            priced += r['requests']
            # OpenAI's listed models have an API long-context rate above 272K;
            # Z.ai's GLM price is flat, so it must not inherit that surcharge.
            input_factor, output_factor = (2, 1.5) if r['long_context'] and rates[3] == 'OpenAI' else (1, 1)
            if r['long_context'] and rates[3] == 'OpenAI':
                model_long[r['model']] = model_long.get(r['model'], 0) + r['requests']
            amount = (input_factor * (r['fresh'] * rates[0] + r['cached'] * rates[1])
                      + output_factor * r['output'] * rates[2]) / 1_000_000
            dollars += amount
            model_costs[r['model']] = model_costs.get(r['model'], 0.0) + amount
        for model in models:
            rates = API_PRICES.get(model['model'])
            model['api_estimate'] = dict(
                dollars=model_costs.get(model['model']),
                provider=rates[3] if rates else None,
                input_per_million=rates[0] if rates else None,
                cached_per_million=rates[1] if rates else None,
                output_per_million=rates[2] if rates else None,
                long_context_requests=model_long.get(model['model'], 0))
        api_estimate = dict(dollars=dollars if priced else None, priced_requests=priced,
                            unpriced_requests=unpriced, price_as_of=API_PRICE_DATE)
        coverage = dict(self.db.execute('''SELECT MIN(ts) first_record, MAX(ts) last_record,
            COUNT(DISTINCT strftime('%Y-%m-%d',ts,'unixepoch','+8 hours')) recorded_days
            FROM usage WHERE ts>=? AND ts<?''', (since, until)).fetchone())
        return dict(key=key, label=label, start=since, end=until, summary=summary,
                    speed_summary=speed, timeline=timeline, models=models, coverage=coverage,
                    api_estimate=api_estimate)

    def snapshot(self, now=None):
        with self.lock:
            now = (now or dt.datetime.now(TZ)).astimezone(TZ)
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            periods = {
                'today': self.period_summary('today', '今日', today, now, 3600),
                '7d': self.period_summary('7d', '最近 7 日', today-dt.timedelta(days=6), now, 86400),
                '30d': self.period_summary('30d', '最近 30 日', today-dt.timedelta(days=29), now, 86400),
            }
            def total(since):
                row = dict(self.db.execute('''SELECT COUNT(*) requests,
                  COUNT(DISTINCT thread) sessions, COALESCE(SUM(input),0) input,
                  COALESCE(SUM(cached),0) cached, COALESCE(SUM(output),0) output,
                  COALESCE(SUM(reasoning),0) reasoning FROM usage WHERE ts>=?''',
                  (since,)).fetchone())
                row['total'] = row['input'] + row['output']
                return row
            daily = []
            for days in range(6, -1, -1):
                start = today - dt.timedelta(days=days)
                row = dict(self.db.execute('''SELECT COALESCE(SUM(input),0) input,
                  COALESCE(SUM(output),0) output FROM usage WHERE ts>=? AND ts<?''',
                  (start.timestamp(), (start + dt.timedelta(days=1)).timestamp())).fetchone())
                row['day'] = start.strftime('%m-%d')
                daily.append(row)
            speeds = [self.speed_record(raw) for raw in self.db.execute(
                'SELECT * FROM speed ORDER BY ts DESC LIMIT 40')]
            latest = next((r for r in speeds if r['tps'] is not None), None)
            if latest is None:
                raw = self.db.execute('''SELECT * FROM speed WHERE duration_ms>0
                    ORDER BY ts DESC LIMIT 1''').fetchone()
                latest = self.speed_record(raw) if raw else None
            recent_cutoff = time.time() - 86400
            summary = dict(self.db.execute('''SELECT COUNT(*) samples,
              COALESCE(SUM(output),0) tokens, COALESCE(SUM(duration_ms),0) ms
              FROM speed WHERE duration_ms>0 AND ts>=?''', (recent_cutoff,)).fetchone())
            summary['tps'] = summary['tokens'] * 1000 / summary['ms'] if summary['ms'] else None
            sessions = [dict(r) for r in self.db.execute('''SELECT thread, MAX(ts) ts,
              COUNT(*) requests, SUM(input) input, SUM(cached) cached,
              SUM(output) output, SUM(reasoning) reasoning FROM usage GROUP BY thread
              ORDER BY ts DESC LIMIT 20''')]
            models = [dict(r) for r in self.db.execute('''SELECT model, COUNT(*) requests,
              SUM(input) input, SUM(cached) cached, SUM(output) output,
              SUM(reasoning) reasoning FROM usage WHERE ts>=? GROUP BY model
              ORDER BY SUM(input)+SUM(output) DESC''', (today.timestamp(),))]
            received = self.db.execute('SELECT value FROM meta WHERE key=?',
                                       ('last_otel_received',)).fetchone()
            active = [dict(r) for r in self.db.execute('''SELECT r.thread, r.model,
                r.started, MAX(r.ts) last_event FROM request_starts r
                WHERE r.ts>MAX(COALESCE((SELECT MAX(s.ts) FROM speed s WHERE s.thread=r.thread),0),
                  COALESCE((SELECT MAX(e.ts) FROM request_ends e WHERE e.thread=r.thread),0))
                AND r.started>=? GROUP BY r.thread HAVING r.ts=MAX(r.ts)
                ORDER BY r.ts DESC''', (time.time() - 3600,))]
            coverage = dict(self.db.execute('SELECT MIN(ts) first_record, MAX(ts) last_record FROM usage').fetchone())
            return dict(today=periods['today']['summary'], all=total(0), daily=daily,
                speeds=speeds, latest=latest, speed_summary=summary, sessions=sessions,
                models=models, periods=periods, coverage=coverage, active_requests=active,
                status=dict(last_scan=self.last_scan, files=self.files,
                scan_error=self.scan_error, last_otel_received=float(received[0]) if received else None),
                generated_at=now.timestamp(), timezone='Asia/Shanghai')

    def speed_record(self, raw):
        row = dict(raw)
        row['tps'] = row['output'] * 1000 / row['duration_ms'] if row['duration_ms'] else None
        # Only attach effort when exactly one usage record reliably matches.
        matches = self.db.execute('''SELECT effort FROM usage
            WHERE thread=? AND ts BETWEEN ? AND ? AND input=? AND output=? LIMIT 2''',
            (row['thread'], row['ts']-2, row['ts']+2, row['input'], row['output'])).fetchall()
        row['effort'] = matches[0]['effort'] if len(matches) == 1 else None
        return row

    def request_history(self, period='today', cursor=None, limit=20, now=None):
        if period not in ('today', '7d', '30d') or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid history range or page size')
        now = (now or dt.datetime.now(TZ)).astimezone(TZ)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start -= dt.timedelta(days={'today': 0, '7d': 6, '30d': 29}[period])
        params = [start.timestamp(), now.timestamp()]
        where = 'ts>=? AND ts<?'
        with self.lock:
            total = self.db.execute('SELECT COUNT(*) FROM speed WHERE '+where, params).fetchone()[0]
            if cursor is not None:
                try:
                    if not isinstance(cursor, str) or len(cursor) > 512:
                        raise ValueError('Invalid cursor')
                    stamp, key = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                    if not epoch(stamp) or not isinstance(key, str) or not key or len(key) > 256:
                        raise ValueError('Invalid cursor')
                except (ValueError, TypeError, UnicodeError) as error:
                    raise ValueError('Invalid history cursor') from error
                where += ' AND (ts<? OR (ts=? AND id<?))'
                params.extend((stamp, stamp, key))
            rows = self.db.execute('SELECT * FROM speed WHERE '+where+
                ' ORDER BY ts DESC,id DESC LIMIT ?', params+[limit+1]).fetchall()
            records = [self.speed_record(row) for row in rows[:limit]]
            next_cursor = None
            if len(rows) > limit:
                last = records[-1]
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps([last['ts'], last['id']]).encode()).decode()
            return dict(period=period, records=records, total=total, next_cursor=next_cursor)


class Scanner:
    def __init__(self, store, roots):
        self.store, self.roots = store, roots
        self.states = {}

    def scan(self):
        paths = set()
        errors = set()
        for root in self.roots:
            try:
                if root.exists():
                    paths.update(root.rglob('*.jsonl'))
            except OSError as error:
                errors.add(type(error).__name__)
        for path in paths:
            try:
                # File I/O and JSON parsing must not block reads of the stats.
                self.read_file(path)
            except FileNotFoundError:
                # Archiving a session can move it during this scan.
                self.states.pop(str(path), None)
            except OSError as error:
                errors.add(type(error).__name__)
        with self.store.lock:
            self.store.db.commit()
            self.store.files = len(paths)
            self.store.last_scan = time.time()
            self.store.scan_error = ', '.join(sorted(errors)) or None
        present = {str(path) for path in paths}
        for stale in self.states.keys() - present:
            del self.states[stale]

    def read_file(self, path):
        stat = path.stat()
        state = self.states.get(str(path))
        if not state or stat.st_size < state['offset'] or stat.st_ino != state['inode']:
            state = dict(offset=0, inode=stat.st_ino, thread=path.stem, model='unknown',
                         turn='', effort=None, modern=False, cumulative=None)
            self.states[str(path)] = state
        if stat.st_size == state['offset']:
            return
        with path.open('rb') as handle:
            handle.seek(state['offset'])
            while True:
                position = handle.tell()
                line = handle.readline()
                if not line or not line.endswith(b'\n'):
                    state['offset'] = position
                    break
                state['offset'] = handle.tell()
                try:
                    self.line(json.loads(line), state)
                except (ValueError, KeyError, TypeError):
                    continue

    def line(self, obj, state):
        if not isinstance(obj, dict) or not isinstance(obj.get('payload'), dict):
            return
        kind = obj.get('type')
        p = obj['payload']
        stamp = epoch(obj.get('timestamp'))
        if kind == 'session_meta':
            if isinstance(p.get('id'), str) and p['id']:
                state['thread'] = p['id']
        elif kind == 'turn_context':
            if isinstance(p.get('model'), str) and p['model']:
                state['model'] = p['model']
            if isinstance(p.get('turn_id'), str):
                state['turn'] = p['turn_id']
            state['effort'] = effort_value(p.get('effort'))
        elif kind == 'event_msg' and p.get('type') == 'task_started':
            turn = p.get('turn_id', state['turn'])
            if not isinstance(turn, str):
                return
            if turn != state['turn']:
                state['effort'] = None
            state['turn'] = turn
        elif kind == 'token_usage_record':
            usage = p.get('usage')
            if (not isinstance(usage, dict) or not usage or not stamp
                    or not isinstance(p.get('response_id'), str) or not p['response_id']):
                return
            turn = p.get('turn_id', state['turn'])
            thread = p.get('thread_id', state['thread'])
            if not isinstance(turn, str) or not isinstance(thread, str):
                return
            state['modern'] = True
            effort = state.get('effort') if turn and turn == state['turn'] and thread == state['thread'] else None
            self.store.add_usage(p['response_id'], thread, turn, stamp, state['model'], usage, effort)
        elif kind == 'event_msg' and p.get('type') == 'token_count' and not state['modern']:
            info = p.get('info') or {}
            if not isinstance(info, dict) or not isinstance(info.get('total_token_usage'), dict):
                return
            cumulative = info['total_token_usage'].get('total_tokens')
            usage = info.get('last_token_usage')
            if (not isinstance(usage, dict) or not usage or not stamp
                    or not isinstance(cumulative, (int, float)) or cumulative == state['cumulative']):
                return
            state['cumulative'] = cumulative
            key = 'legacy:' + hashlib.sha256(('%s|%s|%s' %
                (state['thread'], obj.get('timestamp'), cumulative)).encode()).hexdigest()
            self.store.add_usage(key, state['thread'], state['turn'], stamp, state['model'], usage,
                                 state.get('effort') if state['turn'] else None)

    def run(self):
        while True:
            try:
                self.scan()
            except Exception as error:
                self.store.scan_error = type(error).__name__
            time.sleep(2)


def serve(store, port):
    class Handler(BaseHTTPRequestHandler):
        def allowed(self):
            host = self.headers.get('Host', '')
            if host not in ('127.0.0.1:%d' % port, 'localhost:%d' % port):
                self.send_error(403)
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://127.0.0.1:%d' % port, 'http://localhost:%d' % port):
                self.send_error(403)
                return False
            return True

        def reply(self, value, mime='application/json; charset=utf-8'):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; connect-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self.allowed():
                return
            route = urlparse(self.path).path
            if route == '/api/stats':
                self.reply(store.snapshot())
            elif route == '/api/requests':
                try:
                    query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                    if set(query) - {'period', 'cursor', 'limit'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('Invalid history query')
                    self.reply(store.request_history(period=query.get('period', ['today'])[0],
                        cursor=query.get('cursor', [None])[0], limit=int(query.get('limit', ['20'])[0])))
                except (ValueError, TypeError):
                    self.send_error(400, 'Invalid history query')
            elif route == '/health':
                self.reply({'ok': True, 'service': 'chatgpt-token-monitor', 'version': VERSION})
            elif route in ('/', '/index.html', '/plugin-preview'):
                # The standalone project carries the same UI as its MCP plugin.
                self.reply((ROOT / 'plugin/ui/monitor.html').read_bytes(),
                           'text/html; charset=utf-8')
            else:
                self.send_error(404)

        def do_POST(self):
            if not self.allowed():
                return
            if urlparse(self.path).path != '/v1/logs':
                self.send_error(404)
                return
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 8 * 1024 * 1024:
                    self.send_error(413)
                    return
                raw = self.rfile.read(size)
                if self.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                    if len(raw) > 16 * 1024 * 1024:
                        raise ValueError('Oversized payload')
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError('Invalid payload')
                store.ingest(payload)
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                self.send_error(400)
                return
            self.reply({})

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    http.daemon_threads = True
    print('Codex Token Monitor: http://127.0.0.1:%d' % port, flush=True)
    http.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--codex-dir', type=Path, default=Path.home() / '.codex')
    args = parser.parse_args()
    os.umask(0o077)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(args.data_dir / 'metrics.sqlite3')
    scanner = Scanner(store, [args.codex_dir / 'sessions', args.codex_dir / 'archived_sessions'])
    scanner.scan()
    threading.Thread(target=scanner.run, daemon=True).start()
    serve(store, args.port)


if __name__ == '__main__':
    main()
