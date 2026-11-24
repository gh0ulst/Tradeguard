"""Schedule account reviews from the MyFundedPerps account stream."""
import json
import random
import time
from contextlib import closing

from websocket import (
    ABNF, WebSocketBadStatusException, WebSocketException,
    WebSocketTimeoutException, create_connection,
)


STREAM = 'wss://account-stream.myfundedperpetuals.com/v1/'
MIN_EVENT_INTERVAL = 2


class StreamHalt(SystemExit):
    """Exit with the supervisor's manual-review status."""

    def __init__(self, reason):
        print('Account stream halted:', reason, flush=True)
        super().__init__(42)


def session_scans(socket, account_id, interval, needs_price_check):
    """Yield scan triggers. Each connection starts with a new complete snapshot."""
    seen = False
    ready = False
    healthy = True
    dirty = False
    last_seq = 0
    last_scan = float('-inf')
    last_frame = time.monotonic()
    while True:
        now = time.monotonic()
        if now - last_frame >= 45:
            raise ConnectionError('Stream silent')
        if ready and healthy and ((dirty and now - last_scan >= MIN_EVENT_INTERVAL)
                                  or (needs_price_check() and now - last_scan >= interval)):
            dirty = False
            yield
            last_scan = time.monotonic()
            # Exclude time the consumer spent verifying positions via REST.
            last_frame = last_scan
            continue
        socket.settimeout(1)
        try:
            opcode, data = socket.recv_data(control_frame=True)
        except (TimeoutError, WebSocketTimeoutException):
            continue
        last_frame = time.monotonic()
        if opcode == ABNF.OPCODE_CLOSE:
            if int.from_bytes(data[:2], 'big') == 4001:
                raise StreamHalt('credential expired or revoked')
            raise ConnectionError('Stream closed')
        if opcode != ABNF.OPCODE_TEXT:
            continue
        frame = json.loads(data)
        if not isinstance(frame, dict):
            raise ValueError('Invalid account frame')
        kind = frame.get('type')
        if kind in ('snapshot', 'account', 'order', 'fill', 'position', 'price_alert') or 'seq' in frame:
            seq = frame.get('seq')
            if type(seq) is not int or seq != last_seq + 1:
                raise ConnectionError('Sequence gap')
            last_seq = seq
        if kind == 'heartbeat':
            previous = healthy
            healthy = frame.get('upstream_connected') is not False
            dirty |= healthy and not previous
        elif kind == 'reconnect':
            raise ConnectionError('Server restarting')
        elif kind == 'notice' and frame.get('code') == 'accounts_truncated':
            raise StreamHalt('restrict the key to the accounts being monitored')
        elif kind == 'snapshot_end':
            if account_id and not seen:
                raise StreamHalt('configured account missing from stream snapshot')
            ready = dirty = True
        elif kind in ('snapshot', 'account', 'order', 'fill', 'position'):
            if account_id and frame.get('account_id') != account_id:
                continue
            if account_id and kind == 'account' and frame.get('event') == 'removed':
                raise StreamHalt('configured account removed from stream')
            if kind == 'snapshot':
                seen = True
            dirty = True


def watch_accounts(key, account_id, interval, needs_price_check):
    url = STREAM + ('sandbox/stream' if key.startswith('fp_test_') else 'stream')
    delay = 1
    while True:
        retry_after = 0
        try:
            with closing(create_connection(url, header={'Authorization': 'Bearer ' + key},
                                           timeout=15, redirect_limit=0)) as socket:
                if socket.getstatus() != 101:
                    raise StreamHalt('unexpected WebSocket upgrade response')
                for trigger in session_scans(socket, account_id, interval, needs_price_check):
                    delay = 1
                    yield trigger
        except WebSocketBadStatusException as exc:
            if exc.status_code in (401, 403):
                raise StreamHalt('stream authentication denied') from None
            header = (exc.resp_headers or {}).get('retry-after', '')
            if header.isdigit():
                retry_after = int(header)
        except (OSError, WebSocketException, ValueError):
            pass
        wait = max(delay, retry_after) + random.random()
        print(f'Account stream interrupted. Reconnecting in {wait:.1f}s.', flush=True)
        time.sleep(wait)
        delay = min(60, delay * 2)
