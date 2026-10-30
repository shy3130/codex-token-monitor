#!/usr/bin/env python3
"""Serve the real dashboard with synthetic, in-memory data only."""
import argparse
import datetime as dt
import importlib.util
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
# Import the collector without creating bytecode in the source checkout.
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('token_monitor_demo', ROOT / 'monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def event(name, stamp, thread, model, **values):
    attrs = {'event.name': name, 'event.timestamp': stamp.isoformat(),
             'conversation.id': thread, 'model': model, **values}
    return {'attributes': [{'key': key, 'value': {'stringValue': str(value)}}
                           for key, value in attrs.items()]}


def build_demo():
    store = monitor.Store(':memory:')
    rng = random.Random(42)
    now = dt.datetime.now(monitor.TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    models = ['gpt-6.1-sol', 'gpt-6.1-sol', 'gpt-6.1-sol',
              'gpt-6.1-terra', 'gpt-6-astra']
    records = []
    for days in range(29, -1, -1):
        start = today - dt.timedelta(days=days)
        end = min(start + dt.timedelta(hours=21), now - dt.timedelta(seconds=10))
        available = max(1, (end - start).total_seconds())
        start += dt.timedelta(seconds=min(8 * 3600, available * .35))
        span = max(1, (end - start).total_seconds())
        count = 42 if days == 0 else 22 + (days * 13) % 31
        for index in range(count):
            stamp = start + dt.timedelta(seconds=span * (index + 1) / (count + 1))
            model = models[index % len(models)]
            thread = f'demo-session-{days}-{index // 7}'
            inp = rng.randint(24000, 96000)
            cached = int(inp * rng.uniform(.55, .92))
            output = rng.randint(420, 2600)
            reasoning = int(output * rng.uniform(.2, .65))
            duration = output / rng.uniform(35, 95)
            usage = dict(input_tokens=inp, cached_input_tokens=cached,
                         output_tokens=output, reasoning_output_tokens=reasoning)
            store.add_usage(f'demo-{days}-{index}', thread, f'turn-{index}',
                            stamp.timestamp(), model, usage,
                            ['high', 'xhigh', 'medium'][index % 3])
            records.extend([
                event('codex.websocket_request', stamp - dt.timedelta(seconds=duration),
                      thread, model, success='true'),
                event('codex.sse_event', stamp, thread, model,
                      **{'event.kind': 'response.completed', 'input_token_count': inp,
                         'cached_token_count': cached, 'output_token_count': output,
                         'reasoning_token_count': reasoning}),
            ])
    store.ingest({'resourceLogs': [{'scopeLogs': [{'logRecords': records}]}]})
    return store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8877,
                        help='loopback HTTP port (default: 8877)')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    store = build_demo()
    print('DEMO: synthetic data only; no local sessions or database files.', flush=True)
    try:
        monitor.serve(store, args.port)
    except KeyboardInterrupt:
        pass
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
