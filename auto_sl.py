"""MyFundedPerps automatic SL watcher for open positions and resting entry orders.
Default: read-only.
Live writes require both --execute and AUTO_SL_EXECUTE=true plus terminal confirmation.
Scope one exact account. No blind retries; persistent journal prevents repeated attempts.
Optional Discord alerts use DISCORD_WEBHOOK_URL and never block order management.
"""
import argparse
import atexit
import json
import os
import queue
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from main import (
    Client, LIVE, pages, get_single, proposed_stop, dec, matching_stop,
    protective_stops,
)

JOURNAL = Path(__file__).with_name('auto_sl_attempts.json')
STATE = Path(__file__).with_name('auto_sl_state.json')
ALERT_SUPPRESSION_SECONDS = 900
REPEATED_TIMEOUT_THRESHOLD = 3
REPEATED_API_FAILURE_THRESHOLD = 3
ALERT_QUEUE_CAPACITY = 100
ALERT_GLOBAL_LIMIT = 10
ALERT_GLOBAL_WINDOW_SECONDS = 60
DISCORD_TIMEOUT = (1, 2)


def masked_account(account_id):
    value = str(account_id or '')
    return '****' + value[-4:] if len(value) > 4 else '****'


def safe_alert_text(value, limit=240):
    text = re.sub(r'[\r\n\x00-\x1f]+', ' ', str(value))
    text = text.replace('@', '@\u200b')
    return text[:limit]


class AlertNotifier:
    def __init__(
        self, webhook_url=None, suppression_seconds=ALERT_SUPPRESSION_SECONDS,
        queue_capacity=ALERT_QUEUE_CAPACITY, global_limit=ALERT_GLOBAL_LIMIT,
        global_window_seconds=ALERT_GLOBAL_WINDOW_SECONDS,
    ):
        self.webhook_url = webhook_url
        self.suppression_seconds = suppression_seconds
        self.global_limit = global_limit
        self.global_window_seconds = global_window_seconds
        self.pending = queue.Queue(maxsize=queue_capacity)
        self.last_alert = {}
        self.recent_alert_times = []
        self.dropped_alerts = {}
        self.timeout_count = 0
        self.api_failure_count = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker = None

    def configure(self, webhook_url):
        with self.lock:
            self.webhook_url = webhook_url or None
        if self.webhook_url:
            try:
                self.start()
            except Exception as exc:
                print('Discord notification worker could not start:', type(exc).__name__)

    def start(self):
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                return not self.stop_event.is_set()
            self.stop_event.clear()
            self.worker = threading.Thread(
                target=self._deliver,
                name='discord-notifications',
                daemon=True,
            )
            try:
                self.worker.start()
            except Exception as exc:
                self.worker = None
                print('Discord notification worker could not start:', type(exc).__name__)
                return False
            return True

    def resolve(self, alert_key):
        with self.lock:
            self.last_alert.pop(alert_key, None)

    def alert(self, alert_key, severity, title, position=None, account_id=None, details=None):
        try:
            return self._enqueue_alert(
                alert_key, severity, title, position, account_id, details
            )
        except Exception as exc:
            print('Discord notification could not be queued:', type(exc).__name__)
            return False

    def _enqueue_alert(self, alert_key, severity, title, position, account_id, details):
        now = time.monotonic()
        position = position or {}
        market = position.get('symbol') or position.get('market_id') or 'unknown'
        side = position.get('side') or 'unknown'
        fields = [
            f'Account: {masked_account(account_id or position.get("account_id"))}',
            f'Market: {safe_alert_text(market, 80)}',
            f'Side: {safe_alert_text(side, 32)}',
        ]
        if details:
            fields.append('Details: ' + safe_alert_text(details))
        payload = {'content': safe_alert_text(f'[{severity}] {title}', 180) + '\n' + '\n'.join(fields)}

        with self.lock:
            if not self.webhook_url or self.stop_event.is_set():
                return False
            if self.worker is None or not self.worker.is_alive():
                self.dropped_alerts['worker unavailable'] = (
                    self.dropped_alerts.get('worker unavailable', 0) + 1
                )
                return False
            previous = self.last_alert.get(alert_key)
            if previous is not None and now - previous < self.suppression_seconds:
                return False
            self.recent_alert_times = [
                timestamp for timestamp in self.recent_alert_times
                if now - timestamp < self.global_window_seconds
            ]
            if len(self.recent_alert_times) >= self.global_limit:
                self.dropped_alerts['global alert rate limit'] = (
                    self.dropped_alerts.get('global alert rate limit', 0) + 1
                )
                return False
            try:
                self.pending.put_nowait((self.webhook_url, payload))
            except queue.Full:
                self.dropped_alerts['delivery queue full'] = (
                    self.dropped_alerts.get('delivery queue full', 0) + 1
                )
                return False
            self.last_alert[alert_key] = now
            self.recent_alert_times.append(now)
        return True

    def _deliver(self):
        while not self.stop_event.is_set() or not self.pending.empty():
            self._flush_drop_log()
            try:
                webhook_url, payload = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                response = requests.post(
                    webhook_url, json=payload, timeout=DISCORD_TIMEOUT
                )
                status = getattr(response, 'status_code', 0)
                if not 200 <= status < 300:
                    print('Discord notification delivery failed: HTTP', status)
            except Exception as exc:
                print('Discord notification delivery failed:', type(exc).__name__)
            finally:
                self.pending.task_done()
        self._flush_drop_log()

    def _flush_drop_log(self):
        with self.lock:
            dropped = self.dropped_alerts
            self.dropped_alerts = {}
        for reason, count in dropped.items():
            print(f'Discord notification dropped ({reason}): {count}')

    def shutdown(self, timeout=2):
        self.stop_event.set()
        worker = self.worker
        if worker is not None and worker is not threading.current_thread():
            try:
                worker.join(timeout=max(0, timeout))
            except Exception as exc:
                print('Discord notification worker shutdown failed:', type(exc).__name__)

    def record_api_timeout(self, account_id=None, position=None):
        with self.lock:
            self.timeout_count += 1
            count = self.timeout_count
        if count >= REPEATED_TIMEOUT_THRESHOLD:
            self.alert(
                'repeated-api-timeouts', 'CRITICAL', 'Repeated API timeouts',
                position=position, account_id=account_id,
                details=f'{count} request timeouts observed',
            )

    def record_api_failure(self, error, account_id=None, position=None):
        with self.lock:
            self.api_failure_count += 1
            count = self.api_failure_count
        if count >= REPEATED_API_FAILURE_THRESHOLD:
            error_detail = (
                f'HTTP {error.response.status_code}'
                if isinstance(error, requests.HTTPError) and error.response is not None
                else type(error).__name__
            )
            self.alert(
                'repeated-api-failures', 'CRITICAL', 'Repeated API failures',
                position=position, account_id=account_id,
                details=f'{count} consecutive request failures; latest: {error_detail}',
            )

    def resolve_api_errors(self):
        with self.lock:
            self.timeout_count = 0
            self.api_failure_count = 0
        self.resolve('repeated-api-timeouts')
        self.resolve('repeated-api-failures')

    def resolve_api_timeouts(self):
        self.resolve_api_errors()


NOTIFIER = AlertNotifier()
atexit.register(NOTIFIER.shutdown)


def alert_position_problem(key, title, position, details=None):
    NOTIFIER.alert(
        key, 'CRITICAL', title, position=position,
        account_id=ACCOUNT_ID, details=details,
    )


def alert_safety_halt(alert_key, position, details):
    NOTIFIER.alert(
        alert_key, 'CRITICAL', 'Watcher safety halt', position=position,
        account_id=ACCOUNT_ID, details=details,
    )


def record_api_error(error, position=None):
    if isinstance(error, requests.Timeout):
        NOTIFIER.record_api_timeout(account_id=ACCOUNT_ID, position=position)
    else:
        NOTIFIER.record_api_failure(error, account_id=ACCOUNT_ID, position=position)


def abort(msg):
    raise RuntimeError(msg)


def record_attempt(target_id, price):
    # Written *before* network submission; unknown outcomes require human review.
    journal = {}
    if JOURNAL.exists():
        journal = json.loads(JOURNAL.read_text())
        if not isinstance(journal, dict):
            abort('Attempt journal invalid: inspect before continuing')
    if target_id in journal:
        abort('This target has a prior write attempt in the journal. Manual review required')
    journal[target_id] = {'at': datetime.now(timezone.utc).isoformat(), 'stop_price': str(price), 'status': 'ATTEMPTED_OR_UNKNOWN'}
    temp = JOURNAL.with_suffix('.tmp')
    temp.write_text(json.dumps(journal, indent=2))
    os.replace(temp, JOURNAL)


def load_state():
    if not STATE.exists():
        return {}
    state = json.loads(STATE.read_text())
    if not isinstance(state, dict):
        abort('Management state is invalid; inspect before continuing')
    return state


def save_state(state):
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2))
    os.replace(temp, STATE)


def quote_reference(client, position, market):
    market_id = position['market_id']
    close_side = 'buy' if position['side'] == 'short' else 'sell'
    quote_data = get_single(
        client,
        '/v1/markets/' + quote(str(market_id), safe='') + '/quote',
        {'side': close_side, 'size': position['size']}
    )
    if (quote_data.get('status') != 'ok' or quote_data.get('market_id') != market_id or
            quote_data.get('fillable') is not True or
            quote_data.get('requested_side') != close_side or
            dec(quote_data.get('requested_size')) != dec(position['size'])):
        abort('Invalid market quote for stop management')
    if market.get('provider') in ('ostium', 'veranta'):
        price = dec(quote_data['mid'])
    else:
        price = dec(quote_data['bid'] if position['side'] == 'long' else quote_data['ask'])
    if price <= 0:
        abort('Invalid close-side quote for stop management')
    return price


def positive_stop_price(value):
    price = dec(value)
    if price <= 0:
        abort('Stop price must be positive')
    return price


def management_state(position, stop, current, state):
    pid = position['id']
    entry = dec(position['entry_price'])
    existing = state.get(pid)
    if existing is not None:
        if (existing.get('side') != position['side'] or
                existing.get('market_id') != position['market_id'] or
                dec(existing.get('entry_price')) != entry):
            abort('Position identity or average entry changed; manual review required')
        if existing.get('pending_stop') is not None:
            pending_price = dec(existing['pending_stop'])
            actual_price = dec(stop['trigger_price'])
            if actual_price != pending_price:
                abort('Prior stop update outcome is uncertain; manual review required')
            existing['pending_stop'] = None
            save_state(state)
        if existing.get('blocked') is True:
            abort('Position stop management is blocked pending manual review')
        existing['best_price'] = str(
            max(dec(existing['best_price']), current) if position['side'] == 'long'
            else min(dec(existing['best_price']), current)
        )
        return existing

    journal = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {}
    record = journal.get(pid)
    initial_stop = dec(record['stop_price']) if isinstance(record, dict) and record.get('stop_price') else dec(stop['trigger_price'])
    entry_to_stop = positive_stop_price(initial_stop)
    risk = entry - entry_to_stop if position['side'] == 'long' else entry_to_stop - entry
    if risk <= 0:
        abort('Could not establish a positive initial risk distance')
    result = {
        'entry_price': str(entry),
        'side': position['side'],
        'market_id': position['market_id'],
        'initial_stop': str(initial_stop),
        'risk': str(risk),
        'best_price': str(current),
        'pending_stop': None,
    }
    state[pid] = result
    save_state(state)
    return result


def next_managed_stop(position, stop, state, current, breakeven_r, activation_r, distance_r, tick):
    if position.get('side') not in ('long', 'short'):
        abort('Unknown position side')
    entry = dec(position['entry_price'])
    risk = dec(state['risk'])
    direction = Decimal(1) if position['side'] == 'long' else Decimal(-1)
    best = dec(state['best_price'])
    favorable = (best - entry) * direction
    current_stop = positive_stop_price(stop['trigger_price'])
    target = current_stop

    if favorable >= risk * breakeven_r:
        target = max(target, entry) if direction > 0 else min(target, entry)
    if favorable >= risk * activation_r:
        trail = best - direction * risk * distance_r
        target = max(target, trail) if direction > 0 else min(target, trail)
    rounding = ROUND_FLOOR if direction > 0 else ROUND_CEILING
    target = (target / tick).to_integral_value(rounding=rounding) * tick
    if (direction > 0 and target <= current_stop) or (direction < 0 and target >= current_stop):
        return None
    if (direction > 0 and target >= current) or (direction < 0 and target <= current):
        abort('Tightened stop would already be crossed; manual review required')
    return target


def manage_position_stop(client, position, stop, orders, config, execute, attempted):
    if position.get('side') not in ('long', 'short'):
        abort('Unknown position side')
    if stop.get('type') != 'stop_market':
        print('  SKIP management: only stop-market orders are adjusted automatically')
        return
    same_market_orders = [o for o in orders if o.get('market_id') == position.get('market_id')]
    if len(same_market_orders) != 1 or same_market_orders[0].get('id') != stop.get('id'):
        print('  SKIP management: additional same-market working orders require manual review')
        return

    market = get_single(client, '/v1/markets/' + quote(str(position['market_id']), safe=''))
    if market.get('market_id') != position['market_id']:
        abort('Market data mismatched during stop management')
    tick = dec(market['tick_size'])
    if tick <= 0:
        abort('Invalid market tick size')
    current = quote_reference(client, position, market)
    current_stop = dec(stop['trigger_price'])
    if ((position['side'] == 'long' and current_stop >= current) or
            (position['side'] == 'short' and current_stop <= current)):
        print('  SKIP management: working stop is crossed or at the live quote')
        return
    state = load_state()
    managed = management_state(position, stop, current, state)
    NOTIFIER.resolve('stop-update:' + str(position['id']))
    target = next_managed_stop(
        position, stop, managed, current,
        config['breakeven_r'], config['activation_r'], config['distance_r'], tick
    )
    save_state(state)
    if target is None:
        return

    print('  Move SL', position.get('symbol'), '|', stop.get('trigger_price'), '->', target)
    if not execute:
        print('  DRY RUN: no stop modification submitted')
        return

    fresh_positions, fresh_orders = account_snapshot(client, ACCOUNT_ID)
    fresh_matches = [p for p in fresh_positions if p.get('id') == position['id']]
    fresh_stops = matching_stop(
        fresh_matches[0], fresh_orders, fresh_positions
    ) if len(fresh_matches) == 1 else []
    if len(fresh_stops) != 1 or fresh_stops[0].get('id') != stop.get('id'):
        abort('Position or stop changed immediately before modification')
    latest = fresh_matches[0]
    if any(latest.get(key) != position.get(key) for key in ('size', 'entry_price', 'side', 'market_id')):
        abort('Position changed immediately before stop modification')
    latest_stop = fresh_stops[0]
    if dec(latest_stop['trigger_price']) != dec(stop['trigger_price']):
        abort('Stop changed immediately before modification')
    if len([o for o in fresh_orders if o.get('market_id') == position['market_id']]) != 1:
        abort('Additional same-market order appeared before modification')
    latest_quote = quote_reference(client, latest, market)
    if ((latest['side'] == 'long' and target >= latest_quote) or
            (latest['side'] == 'short' and target <= latest_quote)):
        abort('Stop target crossed before modification')

    body = {
        'expected_position_size': float(dec(latest['size'])),
        'expected_orders': [{
            'order_id': latest_stop['id'],
            'price': float(dec(latest_stop['trigger_price'])),
            'size': float(dec(latest_stop['size'])),
            'execution_type': 'market',
        }],
        'operations': [{'kind': 'modify', 'order_id': latest_stop['id'], 'price': float(target)}],
    }
    managed['pending_stop'] = str(target)
    save_state(state)
    attempt_key = 'manage-stop:' + str(position['id'])
    attempted.add(attempt_key)
    endpoint = '/v1/positions/' + quote(str(position['id']), safe='') + '/exit-orders'
    try:
        response = client.session.put(client.url + endpoint, json=body, timeout=15)
    except requests.RequestException as exc:
        NOTIFIER.alert(
            'stop-update:' + str(position['id']), 'CRITICAL',
            'Stop update outcome is uncertain', position=position,
            account_id=ACCOUNT_ID,
            details=f'No retry was attempted ({type(exc).__name__})',
        )
        abort('STOP UPDATE OUTCOME UNKNOWN. Check the account manually; do NOT retry')
    if response.status_code != 200:
        NOTIFIER.alert(
            'stop-update:' + str(position['id']), 'CRITICAL',
            'Stop update failed', position=position, account_id=ACCOUNT_ID,
            details=f'API returned HTTP {response.status_code}; stop state requires verification',
        )
        abort(f'STOP UPDATE NOT CONFIRMED (HTTP {response.status_code}). Check the account manually; request ID: {response.headers.get("X-Request-Id", "unavailable")}')
    try:
        validate_after(client, latest, target)
    except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as exc:
        NOTIFIER.alert(
            'stop-update:' + str(position['id']), 'CRITICAL',
            'Stop update outcome is uncertain', position=position,
            account_id=ACCOUNT_ID,
            details=f'Update could not be verified ({type(exc).__name__})',
        )
        raise
    managed['pending_stop'] = None
    save_state(state)
    attempted.discard(attempt_key)
    NOTIFIER.resolve('stop-update:' + str(position['id']))
    favorable = (dec(managed['best_price']) - dec(position['entry_price'])) * (
        Decimal(1) if position['side'] == 'long' else Decimal(-1)
    )
    update_kind = (
        'trailing-stop' if favorable >= dec(managed['risk']) * config['activation_r']
        else 'breakeven-stop'
    )
    NOTIFIER.alert(
        f'confirmed-{update_kind}:{position["id"]}:{target}',
        'INFO', f'{update_kind.replace("-", " ").title()} Update Confirmed',
        position=position, account_id=ACCOUNT_ID,
        details=f'Confirmed stop price: {target}',
    )
    NOTIFIER.resolve('no-protective-stop:' + str(position['id']))
    NOTIFIER.resolve('stop-size-mismatch:' + str(position['id']))
    print('  VERIFIED: tightened stop is working')


def account_snapshot(client, aid):
    # API docs: the open positions endpoint returns a complete bounded set.
    positions = pages(client, '/v1/positions', {'account_id': aid, 'status': 'open'}, paginated=False)
    orders = pages(client, '/v1/orders', {'account_id': aid, 'status': 'working', 'limit': 100})
    return positions, orders


def candidate(client, p, orders, positions, percentage):
    pid, mid = p['id'], p['market_id']
    if p['account_id'] != ACCOUNT_ID or p.get('status') != 'open':
        abort('Position not open or not in configured account')
    if matching_stop(p, orders, positions):
        return None, None, 'Working SL candidate already exists'
    # Strict: do not alter the exit order snapshot where ANY same-market order exists.
    ambiguous = [o for o in orders if o.get('target_position_id') == pid or o.get('market_id') == mid]
    if ambiguous:
        abort('Related/ambiguous orders exist; inspect on website')
    market = get_single(client, '/v1/markets/' + quote(str(mid), safe=''))
    if market.get('market_id') != mid:
        abort('Market data mismatched')
    if market.get('trading_enabled') is not True and market.get('reduce_only') is not True:
        abort('Market does not allow trading or reduce-only orders')
    tick, step, size = dec(market['tick_size']), dec(market['size_step']), dec(p['size'])
    if tick <= 0 or step <= 0 or size <= 0 or size % step != 0:
        abort('Invalid size or market increments')
    price = proposed_stop(p, tick, percentage)
    close_side = 'buy' if p['side'] == 'short' else 'sell'
    q = get_single(
        client,
        '/v1/markets/' + quote(str(mid), safe='') + '/quote',
        {'side': close_side, 'size': p['size']}
    )
    if (q.get('status') != 'ok' or q.get('market_id') != mid or
            q.get('fillable') is not True or q.get('requested_side') != close_side or
            dec(q.get('requested_size')) != size):
        abort('Invalid or unfillable size-aware close quote')
    current = dec(q['mid'] if market.get('provider') in ('ostium', 'veranta')
                  else q['ask'] if p['side'] == 'short' else q['bid'])
    if current <= 0 or (p['side'] == 'short' and price <= current) or (p['side'] == 'long' and price >= current):
        abort('Stop is crossed or invalid relative to live quote')
    body = {'expected_position_size': float(size), 'expected_orders': [],
            'operations': [{'kind': 'place', 'group': 'sl', 'price': float(price),
                            'size': float(size), 'execution_type': 'market'}]}
    return price, body, 'READY'


def validate_after(client, p, price):
    positions, orders = account_snapshot(client, ACCOUNT_ID)
    matches = [o for o in matching_stop(p, orders, positions)
               if o.get('type') == 'stop_market' and dec(o.get('trigger_price', 0)) == price]
    if len(matches) != 1:
        abort('No uniquely verified SL found after request. Check website immediately; do NOT retry')
    return matches[0]


def entry_order_stop(order, tick, percentage):
    order_type = order.get('type')
    if order_type == 'limit':
        entry_price = order.get('limit_price')
    elif order_type == 'stop_market':
        entry_price = order.get('trigger_price')
    elif order_type == 'stop_limit':
        entry_price = order.get('limit_price')
    else:
        abort('Unsupported pending entry order type')
    side = {'buy': 'long', 'sell': 'short'}.get(order.get('side'))
    if not side or entry_price is None:
        abort('Pending entry is missing a valid side or entry price')
    return proposed_stop({'entry_price': entry_price, 'side': side}, tick, percentage)


def process_entry_order(client, original, pct, execute, attempted):
    order_id = original.get('id')
    if not order_id:
        return
    journal_id = 'entry-order:' + str(order_id)
    if journal_id in attempted:
        return
    if (original.get('account_id') != ACCOUNT_ID or original.get('status') != 'resting' or
            original.get('type') not in ('limit', 'stop_market', 'stop_limit') or
            original.get('reduce_only') is not False):
        return
    if original.get('attached_stop_loss_price') is not None:
        return

    positions, orders = account_snapshot(client, ACCOUNT_ID)
    matches = [order for order in orders if order.get('id') == order_id]
    if len(matches) != 1:
        print('  SKIP entry order; no longer uniquely working')
        return
    order = matches[0]
    if (order.get('status') != 'resting' or order.get('reduce_only') is not False or
            order.get('type') not in ('limit', 'stop_market', 'stop_limit') or
            order.get('attached_stop_loss_price') is not None):
        print('  SKIP entry order; state changed or an attached SL already exists')
        return
    if any(p.get('market_id') == order.get('market_id') for p in positions):
        print('  SKIP entry order; an open position already exists in this market')
        return
    if any(o.get('id') != order_id and o.get('market_id') == order.get('market_id')
           for o in orders):
        print('  SKIP entry order; another working order makes the market ambiguous')
        return

    market = get_single(client, '/v1/markets/' + quote(str(order['market_id']), safe=''))
    if market.get('market_id') != order.get('market_id'):
        abort('Entry market data mismatched')
    tick = dec(market['tick_size'])
    if tick <= 0:
        abort('Invalid entry market tick size')
    price = entry_order_stop(order, tick, pct)
    if JOURNAL.exists() and journal_id in json.loads(JOURNAL.read_text()):
        print('  SKIP entry order; prior write outcome requires manual review')
        return
    print('  Pending', order.get('symbol'), order.get('side'), order.get('type'),
          '| attached SL:', price)
    if not execute:
        print('  DRY RUN: no attached stop submitted')
        return

    endpoint = '/v1/orders/' + quote(str(order_id), safe='') + '/attached-exits'
    body = {'stop_loss_price': float(price)}
    record_attempt(journal_id, price)
    attempted.add(journal_id)
    try:
        response = client.session.put(client.url + endpoint, json=body, timeout=15)
    except requests.RequestException as exc:
        NOTIFIER.alert(
            'attached-stop-update:' + str(order_id), 'CRITICAL',
            'Attached stop update outcome is uncertain',
            position={
                'market_id': order.get('market_id'),
                'symbol': order.get('symbol'),
                'side': {'buy': 'long', 'sell': 'short'}.get(order.get('side')),
                'account_id': order.get('account_id'),
            },
            account_id=ACCOUNT_ID,
            details=f'No retry was attempted ({type(exc).__name__})',
        )
        abort('ATTACHED-SL WRITE OUTCOME UNKNOWN. Check the order manually; do NOT retry')
    if response.status_code != 200:
        NOTIFIER.alert(
            'attached-stop-update:' + str(order_id), 'CRITICAL',
            'Attached stop update failed',
            position={
                'market_id': order.get('market_id'),
                'symbol': order.get('symbol'),
                'side': {'buy': 'long', 'sell': 'short'}.get(order.get('side')),
                'account_id': order.get('account_id'),
            },
            account_id=ACCOUNT_ID,
            details=f'API returned HTTP {response.status_code}; stop state requires verification',
        )
        abort(f'ATTACHED-SL WRITE NOT CONFIRMED (HTTP {response.status_code}). Check order manually; request ID: {response.headers.get("X-Request-Id", "unavailable")}')
    try:
        verified = get_single(client, '/v1/orders/' + quote(str(order_id), safe=''))
    except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as exc:
        NOTIFIER.alert(
            'attached-stop-update:' + str(order_id), 'CRITICAL',
            'Attached stop update outcome is uncertain',
            position={
                'market_id': order.get('market_id'),
                'symbol': order.get('symbol'),
                'side': {'buy': 'long', 'sell': 'short'}.get(order.get('side')),
                'account_id': order.get('account_id'),
            },
            account_id=ACCOUNT_ID,
            details=f'Update could not be verified ({type(exc).__name__})',
        )
        raise
    if (verified.get('id') != order_id or verified.get('status') != 'resting' or
            dec(verified.get('attached_stop_loss_price')) != price):
        NOTIFIER.alert(
            'attached-stop-update:' + str(order_id), 'CRITICAL',
            'Attached stop update could not be verified',
            position={
                'market_id': order.get('market_id'),
                'symbol': order.get('symbol'),
                'side': {'buy': 'long', 'sell': 'short'}.get(order.get('side')),
                'account_id': order.get('account_id'),
            },
            account_id=ACCOUNT_ID,
            details='API response did not confirm the requested stop',
        )
        abort('Attached SL not uniquely verified. Check the order manually; do NOT retry')
    attempted.discard(journal_id)
    NOTIFIER.resolve('attached-stop-update:' + str(order_id))
    print('  VERIFIED: stop attached to resting entry order')


def process_position(client, original, pct, config, execute, attempted):
    pid = original.get('id')
    if not pid:
        abort('Position missing ID')
    if pid in attempted:
        print('  SKIP: write outcome requires manual review')
        return
    # Always fetch a fresh position and complete working-order set before evaluation.
    positions, orders = account_snapshot(client, ACCOUNT_ID)
    targets = [p for p in positions if p.get('id') == pid]
    if len(targets) != 1:
        NOTIFIER.resolve('no-protective-stop:' + str(pid))
        NOTIFIER.resolve('stop-size-mismatch:' + str(pid))
        NOTIFIER.resolve('unknown-position-side:' + str(pid))
        NOTIFIER.resolve('stop-update:' + str(pid))
        print('  Position changed or closed; skipping')
        return
    p = targets[0]
    try:
        related_stops = protective_stops(p, orders, positions)
        stops = matching_stop(p, orders, positions)
    except ValueError as exc:
        if 'Unknown position side' in str(exc):
            alert_position_problem(
                'unknown-position-side:' + str(pid),
                'Unknown position side encountered', p,
                'Side must be long or short; stop management was skipped',
            )
        raise
    NOTIFIER.resolve('unknown-position-side:' + str(pid))

    mismatched = [stop for stop in related_stops if dec(stop['size']) != dec(p['size'])]
    mismatch_key = 'stop-size-mismatch:' + str(pid)
    missing_key = 'no-protective-stop:' + str(pid)
    if mismatched:
        sizes = ', '.join(str(stop['size']) for stop in mismatched)
        alert_position_problem(
            mismatch_key, 'Protective stop size does not match position', p,
            f'Position size {p["size"]}; stop size(s) {sizes}',
        )
    else:
        NOTIFIER.resolve(mismatch_key)
    if len(stops) != 1:
        reason = (
            'Multiple size-matched protective stops are present'
            if len(stops) > 1 else 'No exactly size-matched protective stop is verified'
        )
        alert_position_problem(missing_key, 'Position has no uniquely verified protective stop', p, reason)
        if len(stops) > 1:
            print('  SKIP: multiple working protective stops; manual review required')
            return
    else:
        NOTIFIER.resolve(missing_key)

    if len(stops) > 1:
        return
    if stops:
        manage_position_stop(client, p, stops[0], orders, config, execute, attempted)
        return

    price, body, status = candidate(client, p, orders, positions, pct)
    if body is None:
        print('  SKIP:', status)
        return
    if JOURNAL.exists() and pid in json.loads(JOURNAL.read_text()):
        print('  SKIP: prior write attempt exists but no matching stop is verified; manual review required')
        return
    print('  Candidate', p.get('symbol'), p.get('side'), 'entry', p.get('entry_price'), 'SL', price)
    if not execute:
        print('  DRY RUN: no order submitted')
        return
    # Repeat the snapshot immediately before *every* write; reject state changes.
    fresh, latest_orders = account_snapshot(client, ACCOUNT_ID)
    same = [x for x in fresh if x.get('id') == pid]
    if len(same) != 1 or any(same[0].get(k) != p.get(k) for k in ('size', 'entry_price', 'side', 'market_id')):
        abort('Position changed immediately before submission')
    next_price, next_body, _ = candidate(client, same[0], latest_orders, fresh, pct)
    if next_body != body or next_price != price:
        abort('Quote or candidate changed before submission')
    record_attempt(pid, price)
    attempted.add(pid)
    endpoint = '/v1/positions/' + quote(str(pid), safe='') + '/exit-orders'
    # PUT endpoint does not define Idempotency-Key: never auto-retry.
    try:
        resp = client.session.put(client.url + endpoint, json=body, timeout=15)
    except requests.RequestException as exc:
        NOTIFIER.alert(
            'stop-update:' + str(pid), 'CRITICAL',
            'Initial protective stop placement outcome is uncertain', position=p,
            account_id=ACCOUNT_ID,
            details=f'No retry was attempted ({type(exc).__name__})',
        )
        abort('WRITE OUTCOME UNKNOWN. Bot halted; check website before any further write')
    if resp.status_code != 200:
        NOTIFIER.alert(
            'stop-update:' + str(pid), 'CRITICAL',
            'Initial protective stop placement failed', position=p,
            account_id=ACCOUNT_ID,
            details=f'API returned HTTP {resp.status_code}; stop state requires verification',
        )
        abort(f'WRITE NOT CONFIRMED (HTTP {resp.status_code}). Bot halted; request ID: {resp.headers.get("X-Request-Id", "unavailable")}')
    try:
        validate_after(client, same[0], price)
    except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as exc:
        NOTIFIER.alert(
            'stop-update:' + str(pid), 'CRITICAL',
            'Initial protective stop placement could not be verified', position=p,
            account_id=ACCOUNT_ID,
            details=f'API response did not confirm the requested stop ({type(exc).__name__})',
        )
        raise
    attempted.discard(pid)
    NOTIFIER.resolve('stop-update:' + str(pid))
    NOTIFIER.resolve('no-protective-stop:' + str(pid))
    NOTIFIER.resolve('stop-size-mismatch:' + str(pid))
    print('  VERIFIED: matching working stop returned by API; also check website')


def run():
    load_dotenv()
    global ACCOUNT_ID
    NOTIFIER.configure(os.getenv('DISCORD_WEBHOOK_URL', '').strip())
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', help='Enable production order submissions after safeguards')
    parser.add_argument('--unattended', action='store_true', help='Allow service startup without terminal confirmation')
    args = parser.parse_args()
    base = os.getenv('MFP_BASE_URL', '').rstrip('/')
    key = os.getenv('MFP_API_KEY', '')
    ACCOUNT_ID = os.getenv('AUTO_SL_ACCOUNT_ID', '').strip()
    if base != LIVE or not key.startswith('fp_live_'):
        raise SystemExit('This version requires the official live production API configuration')
    if not ACCOUNT_ID:
        raise SystemExit('Set AUTO_SL_ACCOUNT_ID to the exact competition account ID in .env')
    pct = dec(os.getenv('SL_PERCENT', '1'))
    if not (Decimal('0') < pct < Decimal('100')):
        raise SystemExit('Invalid SL_PERCENT')
    config = {
        'breakeven_r': dec(os.getenv('BREAKEVEN_R', '1')),
        'activation_r': dec(os.getenv('TRAILING_ACTIVATION_R', '2')),
        'distance_r': dec(os.getenv('TRAILING_DISTANCE_R', '1')),
    }
    if (config['breakeven_r'] <= 0 or config['activation_r'] < config['breakeven_r'] or
            config['distance_r'] <= 0 or config['distance_r'] > config['activation_r']):
        raise SystemExit('Require BREAKEVEN_R > 0, TRAILING_ACTIVATION_R >= BREAKEVEN_R, and 0 < TRAILING_DISTANCE_R <= TRAILING_ACTIVATION_R')
    interval = max(15, int(os.getenv('CHECK_INTERVAL', '30')))
    execute = args.execute
    if execute:
        if os.getenv('AUTO_SL_EXECUTE', '').lower() != 'true':
            raise SystemExit('Write lock: set AUTO_SL_EXECUTE=true only when deliberately authorizing live competition API writes')
        if args.unattended:
            if os.getenv('AUTO_SL_UNATTENDED', '').lower() != 'true':
                raise SystemExit('Unattended lock: set AUTO_SL_UNATTENDED=true only when deliberately authorizing service startup')
            print('PRODUCTION API WRITE MODE — UNATTENDED. This can affect competition account rules/results.')
        else:
            print('PRODUCTION API WRITE MODE. This can affect competition account rules/results.')
        print('Scoped to account ending', ACCOUNT_ID[-4:])
        if not args.unattended and input('Type ENABLE AUTO SL to allow unattended stop orders in this run: ').strip() != 'ENABLE AUTO SL':
            raise SystemExit('Not authorized')
    elif args.unattended:
        raise SystemExit('--unattended requires --execute')
    else:
        print('READ-ONLY WATCH MODE. No HTTP write methods are called.')
    client = Client(base, key)
    accounts = pages(client, '/v1/accounts', {}, paginated=False)
    configured_id = str(ACCOUNT_ID).strip()
    available_ids = [
        str(account.get('id', '')).strip()
        for account in accounts
        if isinstance(account, dict)
    ]
    matches = [
        account_id
        for account_id in available_ids
        if account_id == configured_id
    ]
    print('Accounts returned:', len(available_ids))
    print('Configured ID length:', len(configured_id))
    print('Available ID lengths:', [len(account_id) for account_id in available_ids])
    print('Exact matching accounts:', len(matches))
    if len(matches) != 1:
        raise SystemExit(
            'Account ID mismatch. Copy the full account ID '
            'from get_position_id.py into AUTO_SL_ACCOUNT_ID.'
        )
    attempted = set()
    print('Auto-SL account ending', ACCOUNT_ID[-4:], '| initial SL percent:', pct,
          '| breakeven at:', config['breakeven_r'], 'R | trailing activation:',
          config['activation_r'], 'R | trailing distance:', config['distance_r'],
          'R | interval:', interval)
    try:
        while True:
            print('\nScan', datetime.now().isoformat(timespec='seconds'))
            api_error_seen = False
            try:
                positions, orders = account_snapshot(client, ACCOUNT_ID)
                print('Positions', len(positions), '| working orders', len(orders))
                for p in positions:
                    try:
                        process_position(client, p, pct, config, execute, attempted)
                    except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as exc:
                        if isinstance(exc, requests.RequestException):
                            record_api_error(exc, p)
                            api_error_seen = True
                        if (p.get('id') in attempted or
                                'manage-stop:' + str(p.get('id')) in attempted):
                            alert_safety_halt(
                                'safety-halt:' + str(p.get('id')),
                                p,
                                'A live stop write may have been applied; manual verification is required',
                            )
                            print('HALT: live write outcome uncertain. Manually inspect account before restarting.')
                            raise SystemExit(42)
                        if 'Unknown position side' in str(exc):
                            alert_position_problem(
                                'unknown-position-side:' + str(p.get('id')),
                                'Unknown position side encountered', p,
                                'Side must be long or short; stop management was skipped',
                            )
                        print('  SKIP position; no write attempted:', str(exc)[:200])
                for order in orders:
                    try:
                        process_entry_order(client, order, pct, execute, attempted)
                    except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as exc:
                        if isinstance(exc, requests.RequestException):
                            record_api_error(exc, {
                                'market_id': order.get('market_id'),
                                'symbol': order.get('symbol'),
                                'side': {'buy': 'long', 'sell': 'short'}.get(order.get('side')),
                            })
                            api_error_seen = True
                        if 'entry-order:' + str(order.get('id')) in attempted:
                            alert_safety_halt(
                                'safety-halt:entry-order:' + str(order.get('id')),
                                {
                                    'market_id': order.get('market_id'),
                                    'symbol': order.get('symbol'),
                                    'side': {'buy': 'long', 'sell': 'short'}.get(order.get('side')),
                                },
                                'A live stop write may have been applied; manual verification is required',
                            )
                            print('HALT: attached-SL write outcome uncertain. Manually inspect order before restarting.')
                            raise SystemExit(42)
                        print('  SKIP pending entry; no write attempted:', str(exc)[:200])
            except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError) as exc:
                if isinstance(exc, requests.RequestException):
                    record_api_error(exc)
                    api_error_seen = True
                print('Scan incomplete; no orders submitted during failed account read:', str(exc)[:200])
            if not api_error_seen:
                NOTIFIER.resolve_api_errors()
            time.sleep(interval)
    except KeyboardInterrupt:
        print('\nStopped')


if __name__ == '__main__':
    run()
