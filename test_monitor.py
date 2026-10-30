import importlib.util
import json
from pathlib import Path
import tempfile
import datetime as dt
import threading
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('monitor', Path(__file__).with_name('monitor.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def otel(name, stamp, **extra):
    attrs = {'event.name': name, 'event.timestamp': stamp,
             'conversation.id': 'thread-test', 'model': 'test-model'}
    attrs.update(extra)
    return {'body': None, 'timeUnixNano': '0', 'attributes': [
        {'key': key, 'value': {'stringValue': str(value)}} for key, value in attrs.items()]}


def payload(*records):
    return {'resourceLogs': [{'scopeLogs': [{'logRecords': list(records)}]}]}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.store = m.Store(':memory:')

    def tearDown(self):
        self.store.db.close()

    def test_cached_and_reasoning_are_subsets(self):
        u = dict(input_tokens=1000, cached_input_tokens=800,
                 output_tokens=100, reasoning_output_tokens=60)
        self.store.add_usage('r1', 't', 'turn', 1, 'model', u)
        self.store.add_usage('r1', 't', 'turn', 1, 'model', u)
        result = self.store.snapshot()['all']
        self.assertEqual((result['requests'], result['total']), (1, 1100))
        self.assertEqual((result['cached'], result['reasoning']), (800, 60))

    def test_real_export_shape_and_duplicate_delivery(self):
        start = otel('codex.websocket_request', '2026-10-29T02:00:00Z',
                     success='true', duration_ms='100')
        end = otel('codex.sse_event', '2026-10-29T02:00:02Z',
                   **{'event.kind': 'response.completed', 'input_token_count': '1000',
                      'output_token_count': '100', 'reasoning_token_count': '20',
                      'cached_token_count': '800', 'ttft_ms': '700',
                      'user.email': 'DO_NOT_STORE', 'prompt': 'DO_NOT_STORE'})
        self.store.ingest(payload(start, end))
        self.store.ingest(payload(start, end))
        result = self.store.snapshot()
        self.assertEqual(len(result['speeds']), 1)
        self.assertAlmostEqual(result['latest']['duration_ms'], 2100, places=2)
        self.assertAlmostEqual(result['latest']['tps'], 100 / 2.1, places=3)
        self.assertNotIn('DO_NOT_STORE', json.dumps(result))
        self.assertEqual(result['all']['total'], 0)  # Timing never double-counts usage.

    def test_no_start_does_not_guess_tps(self):
        self.store.ingest(payload(otel('codex.sse_event', '2026-10-29T02:00:02Z',
            **{'event.kind': 'response.completed', 'output_token_count': '30'})))
        self.assertIsNone(self.store.snapshot()['speeds'][0]['tps'])

    def test_tool_time_between_requests_excluded(self):
        events = []
        for start, end in [('02:00:00', '02:00:02'), ('02:10:00', '02:10:02')]:
            events.extend([otel('codex.websocket_request', '2026-10-29T' + start + 'Z', success='true'),
                otel('codex.sse_event', '2026-10-29T' + end + 'Z',
                     **{'event.kind': 'response.completed', 'output_token_count': '100'})])
        self.store.ingest(payload(*events))
        self.assertEqual([r['tps'] for r in self.store.snapshot()['speeds']], [50, 50])

    def test_warmup_does_not_become_speed_sample(self):
        self.store.ingest(payload(otel('codex.websocket_request', '2026-10-29T02:00:00Z', success='true'),
            otel('codex.sse_event', '2026-10-29T02:00:02Z',
                 **{'event.kind': 'response.completed', 'output_token_count': '0'})))
        self.assertEqual(self.store.snapshot()['speeds'], [])

    def test_partial_line_modern_legacy_and_restart_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'rollout.jsonl'
            usage = dict(input_tokens=1000, output_tokens=100, cached_input_tokens=800)
            rows = [dict(type='session_meta', payload={'id': 'thread'}),
                dict(type='turn_context', payload={'model': 'model'}),
                dict(type='token_usage_record', timestamp='2026-10-29T02:00:00Z',
                     payload={'response_id': 'r1', 'usage': usage}),
                dict(type='event_msg', timestamp='2026-10-29T02:00:01Z',
                     payload={'type': 'token_count', 'info': {
                         'last_token_usage': usage, 'total_token_usage': {'total_tokens': 1100}}})]
            encoded = '\n'.join(map(json.dumps, rows)) + '\n'
            path.write_text(encoded[:-8])
            scanner = m.Scanner(self.store, [root])
            scanner.scan()
            with path.open('a') as f:
                f.write(encoded[-8:])
            scanner.scan()
            m.Scanner(self.store, [root]).scan()
            self.assertEqual(self.store.snapshot()['all']['requests'], 1)
            self.assertEqual(self.store.snapshot()['all']['total'], 1100)

    def test_legacy_repeated_cumulative_is_not_counted_twice(self):
        scanner = m.Scanner(self.store, [])
        state = dict(modern=False, cumulative=None, thread='t', turn='', model='old')
        p = {'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': 5,
             'output_tokens': 2}, 'total_token_usage': {'total_tokens': 7}}}
        for stamp in ['2026-10-29T02:00:00Z', '2026-10-29T02:00:01Z']:
            scanner.line({'type': 'event_msg', 'timestamp': stamp, 'payload': p}, state)
        self.assertEqual(self.store.snapshot()['all']['total'], 7)

    def test_reversed_delivery_recovers_timing(self):
        end = otel('codex.sse_event', '2026-10-29T02:00:02Z',
                   **{'event.kind': 'response.completed', 'output_token_count': '100'})
        self.store.ingest(payload(end))
        self.assertIsNone(self.store.snapshot()['latest'])
        self.store.ingest(payload(otel('codex.websocket_request',
            '2026-10-29T02:00:00Z', success='true')))
        self.assertEqual(self.store.snapshot()['latest']['tps'], 50)

    def test_start_survives_collector_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / 'metrics.sqlite3'
            first = m.Store(db)
            first.ingest(payload(otel('codex.websocket_request',
                '2026-10-29T02:00:00Z', success='true')))
            first.db.close()
            second = m.Store(db)
            second.ingest(payload(otel('codex.sse_event', '2026-10-29T02:00:03Z',
                **{'event.kind': 'response.completed', 'output_token_count': '90'})))
            self.assertEqual(second.snapshot()['latest']['tps'], 30)
            second.db.close()

    def test_live_request_state_and_duplicate_delivery(self):
        def stamp(offset):
            return dt.datetime.fromtimestamp(time.time() + offset,
                dt.timezone.utc).isoformat()
        batch = payload(otel('codex.websocket_request', stamp(-602), success='true'),
            otel('codex.sse_event', stamp(-600),
                **{'event.kind': 'response.completed', 'output_token_count': '100'}))
        self.store.ingest(batch)
        self.store.ingest(batch)
        self.assertEqual(self.store.snapshot()['active_requests'], [])
        self.store.ingest(payload(otel('codex.websocket_request', stamp(-10), success='true')))
        result = self.store.snapshot()
        self.assertEqual(len(result['active_requests']), 1)
        self.assertIsNotNone(result['latest']['tps'])  # Idle samples remain visible.

    def test_delayed_warmup_start_is_not_active(self):
        now = dt.datetime.now(dt.timezone.utc)
        self.store.ingest(payload(otel('codex.sse_event', now.isoformat(),
            **{'event.kind': 'response.completed', 'output_token_count': '0'})))
        self.store.ingest(payload(otel('codex.websocket_request',
            (now - dt.timedelta(seconds=2)).isoformat(), success='true')))
        self.assertEqual(self.store.snapshot()['active_requests'], [])
        self.assertEqual(self.store.snapshot()['speeds'], [])

    def test_missing_start_cannot_reuse_earlier_request(self):
        self.store.ingest(payload(otel('codex.websocket_request',
            '2026-10-29T02:00:00Z', success='true'),
            otel('codex.sse_event', '2026-10-29T02:00:02Z',
                **{'event.kind': 'response.completed', 'output_token_count': '100'}),
            otel('codex.sse_event', '2026-10-29T02:20:00Z',
                **{'event.kind': 'response.completed', 'output_token_count': '100'})))
        result = self.store.snapshot()['speeds']
        self.assertIsNone(result[0]['tps'])
        self.assertEqual(result[1]['tps'], 50)

    def test_period_boundaries_use_beijing_days_and_do_not_count_future(self):
        now = dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ)
        dates = ['2026-09-29T23:59:59+08:00', '2026-09-30T00:00:00+08:00',
                 '2026-10-22T23:59:59+08:00', '2026-10-23T00:00:00+08:00',
                 '2026-10-28T23:59:59+08:00', '2026-10-28T16:00:00Z',
                 '2026-10-29T11:00:00+08:00', '2026-10-29T12:00:00+08:00']
        for i, stamp in enumerate(dates):
            self.store.add_usage(str(i), 'thread', 'turn', m.epoch(stamp), 'model',
                dict(input_tokens=100, cached_input_tokens=80, output_tokens=10, reasoning_output_tokens=4))
        periods = self.store.snapshot(now=now)['periods']
        for key, expected in [('today', 2), ('7d', 4), ('30d', 6)]:
            p = periods[key]
            self.assertEqual(p['summary']['requests'], expected)
            self.assertEqual(p['summary']['total'], expected * 110)
            self.assertEqual(p['summary']['fresh'], expected * 20)
            self.assertEqual(sum(row['total'] for row in p['timeline']), expected * 110)
            self.assertEqual(p['summary']['cache_hit_rate'], 0.8)
        self.assertEqual(len(periods['today']['timeline']), 12)
        self.assertEqual(len(periods['7d']['timeline']), 7)
        self.assertEqual(len(periods['30d']['timeline']), 30)

    def test_period_tps_is_weighted_by_request_time(self):
        now = dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ)
        self.store.ingest(payload(
            otel('codex.websocket_request', '2026-10-29T02:00:00Z', success='true'),
            otel('codex.sse_event', '2026-10-29T02:00:01Z',
                 **{'event.kind': 'response.completed', 'output_token_count': '100'}),
            otel('codex.websocket_request', '2026-10-28T02:00:00Z', success='true'),
            otel('codex.sse_event', '2026-10-28T02:00:09Z',
                 **{'event.kind': 'response.completed', 'output_token_count': '100'})))
        p = self.store.snapshot(now=now)['periods']
        self.assertEqual(p['today']['speed_summary']['tps'], 100)
        self.assertEqual(p['7d']['speed_summary']['samples'], 2)
        self.assertEqual(p['7d']['speed_summary']['tps'], 20)
        self.assertEqual(p['30d']['speed_summary']['tps'], 20)

    def test_empty_period_has_no_invented_tps(self):
        p = self.store.snapshot(now=dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ))['periods']['30d']
        self.assertEqual(p['summary']['total'], 0)
        self.assertEqual(p['coverage']['recorded_days'], 0)
        self.assertIsNone(p['coverage']['first_record'])
        self.assertIsNone(p['speed_summary']['tps'])

    def test_model_tps_uses_own_complete_timings_and_selected_period(self):
        now = dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ)
        cases = [
            ('a-fast', 'model-a', '2026-10-29T02:00:00Z', 1, 100),
            ('a-slow', 'model-a', '2026-10-29T02:01:00Z', 30, 900),
            ('b-only', 'model-b', '2026-10-29T02:02:00Z', 2, 100),
            ('a-old', 'model-a', '2026-10-28T02:00:00Z', 9, 900),
            ('a-incomplete', 'model-a', '2026-10-29T02:03:00Z', None, 500),
        ]
        for thread, model, stamp, seconds, out in cases:
            end = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            self.store.add_usage(thread, thread, 'turn', end.timestamp(), model,
                                 dict(input_tokens=1000, output_tokens=out))
            records = [otel('codex.sse_event', end.isoformat(), model=model,
                           **{'conversation.id': thread, 'event.kind': 'response.completed',
                              'input_token_count': 1000, 'output_token_count': out})]
            if seconds is not None:
                records.insert(0, otel('codex.websocket_request',
                    (end-dt.timedelta(seconds=seconds)).isoformat(), model=model,
                    success='true', **{'conversation.id': thread}))
            self.store.ingest(payload(*records))
        periods = self.store.snapshot(now=now)['periods']
        today = {row['model']: row for row in periods['today']['models']}
        week = {row['model']: row for row in periods['7d']['models']}
        self.assertEqual(today['model-a']['speed_summary']['samples'], 2)
        self.assertEqual(today['model-a']['speed_summary']['tokens'], 1000)
        self.assertAlmostEqual(today['model-a']['speed_summary']['tps'], 1000/31)
        self.assertEqual(today['model-b']['speed_summary']['tps'], 50)
        self.assertEqual(week['model-a']['speed_summary']['samples'], 3)
        self.assertEqual(week['model-a']['speed_summary']['tps'], 1900/40)
        self.assertAlmostEqual(sum(row['token_share'] for row in today.values()), 1)
        self.assertEqual(today['model-a']['total'], 4500)

    def test_model_without_timing_or_with_zero_tokens_has_no_invented_speed(self):
        now = dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ)
        self.store.add_usage('zero', 'thread', 'turn', m.epoch('2026-10-29T02:00:00Z'),
                             'model', dict(input_tokens=0, output_tokens=0))
        model = self.store.snapshot(now=now)['periods']['today']['models'][0]
        self.assertEqual(model['speed_summary'], dict(samples=0, tokens=0, ms=0, tps=None))
        self.assertEqual(model['token_share'], 0)

    def test_reasoning_effort_is_backfilled_and_matched_to_the_same_request(self):
        scanner = m.Scanner(self.store, [])
        state = dict(thread='thread-test', model='model', turn='', effort=None,
                     modern=False, cumulative=None)
        stamp = '2026-10-29T02:00:02Z'
        usage = dict(input_tokens=1000, cached_input_tokens=800,
                     output_tokens=100, reasoning_output_tokens=20)
        self.store.add_usage('r1', 'thread-test', 'turn-1', m.epoch(stamp), 'model', usage)
        scanner.line(dict(type='turn_context', payload={'turn_id': 'turn-1',
                     'model': 'model', 'effort': 'xhigh'}), state)
        scanner.line(dict(type='token_usage_record', timestamp=stamp,
                     payload={'response_id': 'r1', 'turn_id': 'turn-1', 'usage': usage}), state)
        self.store.ingest(payload(otel('codex.sse_event', stamp,
                          **{'event.kind': 'response.completed', 'input_token_count': 1000,
                             'output_token_count': 100})))
        result = self.store.snapshot()
        self.assertEqual(result['all']['requests'], 1)  # Backfill does not add usage.
        self.assertEqual(result['speeds'][0]['effort'], 'xhigh')

    def test_reasoning_effort_is_unknown_when_match_is_missing_or_ambiguous(self):
        stamp = '2026-10-29T02:00:02Z'
        usage = dict(input_tokens=1000, output_tokens=100)
        self.store.ingest(payload(otel('codex.sse_event', stamp,
                          **{'event.kind': 'response.completed', 'input_token_count': 1000,
                             'output_token_count': 100})))
        self.assertIsNone(self.store.snapshot()['speeds'][0]['effort'])
        self.store.add_usage('one', 'thread-test', 'a', m.epoch(stamp), 'model', usage, 'high')
        self.store.add_usage('two', 'thread-test', 'b', m.epoch(stamp), 'model', usage, 'max')
        self.assertIsNone(self.store.snapshot()['speeds'][0]['effort'])

    def test_reasoning_effort_does_not_leak_between_turns_or_accept_arbitrary_text(self):
        scanner = m.Scanner(self.store, [])
        state = dict(thread='thread-test', model='model', turn='', effort=None,
                     modern=False, cumulative=None)
        usage = dict(input_tokens=1, output_tokens=2)
        scanner.line(dict(type='turn_context', payload={'turn_id': 'turn-1', 'effort': 'max'}), state)
        scanner.line(dict(type='token_usage_record', timestamp='2026-10-29T02:00:02Z',
                     payload={'response_id': 'r1', 'turn_id': 'another-turn', 'usage': usage}), state)
        scanner.line(dict(type='event_msg', payload={'type': 'task_started', 'turn_id': 'turn-2'}), state)
        scanner.line(dict(type='token_usage_record', timestamp='2026-10-29T02:00:03Z',
                     payload={'response_id': 'r2', 'turn_id': 'turn-2', 'usage': usage}), state)
        scanner.line(dict(type='turn_context', payload={'turn_id': 'turn-3', 'effort': 'PRIVATE TEXT'}), state)
        scanner.line(dict(type='token_usage_record', timestamp='2026-10-29T02:00:04Z',
                     payload={'response_id': 'r3', 'turn_id': 'turn-3', 'usage': usage}), state)
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT effort FROM usage')],
                         [None, None, None])

    def test_existing_database_migrates_without_losing_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            import sqlite3
            path = Path(directory) / 'old.sqlite3'
            db = sqlite3.connect(path)
            db.executescript('''CREATE TABLE usage (id TEXT PRIMARY KEY, thread TEXT, turn TEXT,
                ts REAL, model TEXT, input INTEGER, cached INTEGER, output INTEGER, reasoning INTEGER);
                INSERT INTO usage VALUES ('r','t','turn',1,'model',10,3,2,1);''')
            db.close()
            store = m.Store(path)
            self.assertEqual(store.snapshot()['all']['total'], 12)
            self.assertIsNone(store.db.execute('SELECT effort FROM usage').fetchone()[0])
            store.db.close()

    def test_api_estimate_uses_cache_and_long_context_rates_without_double_counting(self):
        now = dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ)
        stamp = m.epoch('2026-10-29T10:00:00+08:00')
        self.store.add_usage('short', 't', 'a', stamp, 'gpt-6.1-sol',
            dict(input_tokens=100000, cached_input_tokens=80000, output_tokens=10000))
        self.store.add_usage('long', 't', 'b', stamp, 'gpt-6.1-sol',
            dict(input_tokens=1000000, cached_input_tokens=600000, output_tokens=100000))
        self.store.add_usage('unknown', 't', 'c', stamp, 'unknown-model',
            dict(input_tokens=100, output_tokens=100))
        estimate = self.store.snapshot(now=now)['periods']['today']['api_estimate']
        self.assertAlmostEqual(estimate['dollars'], 0.148 + 3.22)
        self.assertEqual((estimate['priced_requests'], estimate['unpriced_requests']), (2, 1))
        self.assertEqual(estimate['price_as_of'], '2026-10-29')

    def test_api_estimate_is_unavailable_not_zero_if_no_models_are_priced(self):
        now = dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ)
        self.store.add_usage('x', 't', 'a', m.epoch('2026-10-29T10:00:00+08:00'),
                             'unknown-model', dict(input_tokens=100, output_tokens=10))
        estimate = self.store.snapshot(now=now)['periods']['today']['api_estimate']
        self.assertIsNone(estimate['dollars'])
        self.assertEqual(estimate['unpriced_requests'], 1)

    def test_per_model_api_estimates_sum_and_respect_vendor_context_rules(self):
        now = dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ)
        stamp = m.epoch('2026-10-29T10:00:00+08:00')
        usage = dict(input_tokens=1000000, cached_input_tokens=600000, output_tokens=100000)
        self.store.add_usage('openai', 't', 'a', stamp, 'gpt-6.1-sol', usage)
        self.store.add_usage('glm', 't', 'b', stamp, 'glm-5.3', usage)
        self.store.add_usage('internal', 't', 'c', stamp, 'codex-auto-review', usage)
        period = self.store.snapshot(now=now)['periods']['today']
        rows = {row['model']: row['api_estimate'] for row in period['models']}
        self.assertAlmostEqual(rows['gpt-6.1-sol']['dollars'], 3.22)
        self.assertAlmostEqual(rows['glm-5.3']['dollars'], 1.156)  # Z.ai has no OpenAI surcharge.
        self.assertEqual(rows['gpt-6.1-sol']['long_context_requests'], 1)
        self.assertEqual(rows['glm-5.3']['long_context_requests'], 0)
        self.assertEqual(rows['glm-5.3']['provider'], 'Z.ai')
        self.assertEqual(rows['glm-5.3']['cached_per_million'], 0.26)
        self.assertIsNone(rows['codex-auto-review']['dollars'])
        self.assertAlmostEqual(period['api_estimate']['dollars'], 4.376)
        self.assertEqual(period['api_estimate']['unpriced_requests'], 1)
        self.assertNotIn('account_quota', self.store.snapshot(now=now))

    def test_otlp_nanosecond_timestamps_work_without_event_timestamp(self):
        start = otel('codex.websocket_request', '', success='true')
        end = otel('codex.sse_event', '', **{'event.kind': 'response.completed',
                                         'output_token_count': 100})
        start['timeUnixNano'] = '1793239200000000000'
        end['observedTimeUnixNano'] = '1793239202000000000'
        self.store.ingest(payload(start, end))
        self.assertEqual(self.store.snapshot()['latest']['tps'], 50)

    def test_late_boundaries_correct_already_assigned_request_timings(self):
        self.store.ingest(payload(
            otel('codex.websocket_request', '2026-10-29T02:00:00Z', success='true'),
            otel('codex.sse_event', '2026-10-29T02:00:10Z',
                 **{'event.kind': 'response.completed', 'output_token_count': 100})))
        self.store.ingest(payload(otel('codex.sse_event', '2026-10-29T02:00:02Z',
            **{'event.kind': 'response.completed', 'output_token_count': 100})))
        rows = self.store.snapshot()['speeds']
        self.assertIsNone(rows[0]['tps'])  # Later request has no start yet.
        self.assertEqual(rows[1]['tps'], 50)
        self.store.ingest(payload(otel('codex.websocket_request',
            '2026-10-29T02:00:08Z', success='true')))
        self.assertEqual([r['tps'] for r in self.store.snapshot()['speeds']], [50, 50])

    def test_latest_speed_survives_many_requests_with_missing_starts(self):
        base = dt.datetime(2026, 10, 29, 2, tzinfo=dt.timezone.utc)
        self.store.ingest(payload(
            otel('codex.websocket_request', base.isoformat(), success='true'),
            otel('codex.sse_event', (base + dt.timedelta(seconds=2)).isoformat(),
                 **{'event.kind': 'response.completed', 'output_token_count': 100})))
        self.store.ingest(payload(*(otel('codex.sse_event',
            (base + dt.timedelta(seconds=second)).isoformat(),
            **{'event.kind': 'response.completed', 'output_token_count': 100})
            for second in range(3, 45))))
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot['speeds']), 40)
        self.assertTrue(all(r['tps'] is None for r in snapshot['speeds']))
        self.assertEqual(snapshot['latest']['tps'], 50)

    def test_identical_completion_times_cannot_reuse_one_request_start(self):
        self.store.ingest(payload(
            otel('codex.websocket_request', '2026-10-29T02:00:00Z', success='true'),
            otel('codex.sse_event', '2026-10-29T02:00:02Z',
                 **{'event.kind': 'response.completed', 'output_token_count': 100}),
            otel('codex.sse_event', '2026-10-29T02:00:02Z',
                 **{'event.kind': 'response.completed', 'output_token_count': 200})))
        measured = [r for r in self.store.snapshot()['speeds'] if r['tps'] is not None]
        self.assertEqual(len(measured), 1)

    def test_malformed_log_records_do_not_stop_valid_usage_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            rows = [[], None, 'unexpected', {'type': 'turn_context', 'payload': [1]},
                {'type': 'token_usage_record', 'payload': {'response_id': 'bad', 'usage': [1]}},
                {'type': 'token_usage_record', 'timestamp': '2026-10-29T02:00:00Z',
                 'payload': {'response_id': 'good', 'usage': {'input_tokens': 1, 'output_tokens': 2}}}]
            path.write_text('\n'.join(map(json.dumps, rows)) + '\n')
            m.Scanner(self.store, [Path(directory)]).scan()
        self.assertEqual(self.store.snapshot()['all']['total'], 3)

    def test_missing_or_unreadable_file_does_not_block_other_sessions(self):
        for error in (FileNotFoundError, PermissionError):
            with self.subTest(error=error.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'gone.jsonl').write_text('')
                (root / 'valid.jsonl').write_text(json.dumps({
                    'type': 'token_usage_record', 'timestamp': '2026-10-29T02:00:00Z',
                    'payload': {'response_id': 'good', 'usage': {'input_tokens': 1, 'output_tokens': 2}}}) + '\n')
                scanner = m.Scanner(self.store, [root])
                original_read = scanner.read_file
                def read(path):
                    if path.name == 'gone.jsonl':
                        raise error('test file unavailable')
                    original_read(path)
                with mock.patch.object(scanner, 'read_file', side_effect=read):
                    scanner.scan()
                self.assertEqual(self.store.snapshot()['all']['total'], 3)
                self.assertEqual(self.store.scan_error,
                                 None if error is FileNotFoundError else 'PermissionError')

    def test_slow_file_io_does_not_block_stat_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'slow.jsonl').write_text('')
            scanner = m.Scanner(self.store, [root])
            entered, release, responded = threading.Event(), threading.Event(), threading.Event()
            def slow_read(_):
                entered.set()
                release.wait(3)
            def read_stats():
                self.store.snapshot()
                responded.set()
            with mock.patch.object(scanner, 'read_file', side_effect=slow_read):
                worker = threading.Thread(target=scanner.scan)
                reader = threading.Thread(target=read_stats)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    reader.start()
                    self.assertTrue(responded.wait(1), 'Stats blocked while a log file was being read')
                finally:
                    release.set()
                    worker.join(3)
                    if reader.ident is not None:
                        reader.join(3)

    def test_history_pages_all_records_without_duplicates_when_new_records_arrive(self):
        base = dt.datetime(2026, 10, 29, 2, tzinfo=dt.timezone.utc)
        now = dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ)
        # Equal timestamps exercise the secondary sort key across page boundaries.
        self.store.ingest(payload(*(otel('codex.sse_event', base.isoformat(),
            **{'event.kind': 'response.completed', 'output_token_count': n}) for n in range(1, 56))))
        page = self.store.request_history(limit=20, now=now)
        seen = [r['id'] for r in page['records']]
        self.assertEqual(page['total'], 55)
        self.store.ingest(payload(otel('codex.sse_event', (base+dt.timedelta(seconds=1)).isoformat(),
            **{'event.kind': 'response.completed', 'output_token_count': 100})))
        while page['next_cursor']:
            page = self.store.request_history(cursor=page['next_cursor'], limit=20, now=now)
            seen.extend(r['id'] for r in page['records'])
        self.assertEqual(len(seen), 55)
        self.assertEqual(len(set(seen)), 55)
        self.assertEqual(self.store.request_history(limit=20, now=now)['total'], 56)

    def test_history_respects_period_and_rejects_invalid_cursors(self):
        now = dt.datetime(2026, 10, 29, 12, tzinfo=m.TZ)
        self.store.ingest(payload(otel('codex.sse_event', '2026-10-28T02:00:00Z',
            **{'event.kind': 'response.completed', 'output_token_count': 10})))
        self.assertEqual(self.store.request_history(now=now)['total'], 0)
        self.assertEqual(self.store.request_history(period='7d', now=now)['total'], 1)
        for params in ({'cursor': 'bad!'}, {'limit': True}, {'limit': 101}, {'period': 'all'}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.store.request_history(**params)


if __name__ == '__main__':
    unittest.main()
