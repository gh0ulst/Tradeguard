"""MyFundedPerps read-only SL monitor and DRY-RUN request preview. No HTTP writes."""
import argparse
import json
import os
import time
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from urllib.parse import quote

import requests
from dotenv import load_dotenv

LIVE = 'https://developers.myfundedperpetuals.com'
SANDBOX = 'https://sandbox.myfundedperpetuals.com'


def dec(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite number')
    return result


def proposed_stop(position, tick, percentage):
    entry, step, pct = dec(position['entry_price']), dec(tick), dec(percentage)
    if entry <= 0 or step <= 0 or not (0 < pct < 100):
        raise ValueError('Invalid SL inputs')
    side = position['side']
    if side == 'short':
        raw = entry * (Decimal(1) + pct / 100)
        result = (raw / step).to_integral_value(rounding=ROUND_CEILING) * step
    elif side == 'long':
        raw = entry * (Decimal(1) - pct / 100)
        result = (raw / step).to_integral_value(rounding=ROUND_FLOOR) * step
    else:
        raise ValueError('Unknown side')
    if result <= 0:
        raise ValueError('Nonpositive SL')
    return result


def protective_stops(position, orders, positions=None):
    """Return correctly associated protective stop orders, regardless of size."""
    pid, aid, mid = position['id'], position['account_id'], position['market_id']
    if position.get('side') not in ('long', 'short'):
        raise ValueError('Unknown position side')
    side = 'buy' if position['side'] == 'short' else 'sell'
    direction = 'above' if position['side'] == 'short' else 'below'
    same_market_positions = [
        p for p in (positions or [])
        if p.get('account_id') == aid and p.get('market_id') == mid and p.get('status') == 'open'
    ]
    unlinked_is_unambiguous = (
        len(same_market_positions) == 1 and same_market_positions[0].get('id') == pid
    )
    candidates = []
    for order in orders:
        target_id = order.get('target_position_id')
        if ((target_id != pid and not (target_id is None and unlinked_is_unambiguous)) or
            order.get('account_id') != aid or order.get('market_id') != mid or
            order.get('side') != side or order.get('reduce_only') is not True or
            order.get('status') not in ('resting','pending') or
            order.get('cancel_requested_at') is not None or
            order.get('trigger_direction') not in (None, direction) or
            order.get('type') not in ('stop_market', 'stop_limit') or
            order.get('bracket_role') not in (None, 'stop_loss')):
            continue
        try:
            trigger = dec(order['trigger_price'])
            size = dec(order['size'])
            if trigger <= 0 or size <= 0:
                continue
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        candidates.append(order)
    return candidates


def matching_stop(position, orders, positions=None):
    """Conservative: return size-matched candidates, not proof of protection."""
    need = dec(position['size'])
    return [
        order for order in protective_stops(position, orders, positions)
        if dec(order['size']) == need
    ]


def fetch(client, endpoint, params=None):
    response = client.get(endpoint, params=params, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or 'data' not in data:
        raise ValueError('Unexpected API response')
    return data


def pages(client, endpoint, params, paginated=True):
    items, cursor, seen = [], None, set()
    for _ in range(100):
        query = dict(params)
        if cursor:
            query['cursor'] = cursor
        payload = fetch(client, endpoint, query)
        batch = payload['data']
        if not isinstance(batch, list):
            raise ValueError('Expected list')
        items.extend(batch)
        if not paginated or payload.get('has_more') is not True:
            return items
        cursor = payload.get('next_cursor')
        if not cursor or cursor in seen:
            raise ValueError('Invalid pagination cursor')
        seen.add(cursor)
    raise ValueError('Pagination exceeded safe page limit')


def get_single(client, endpoint, params=None):
    item = fetch(client, endpoint, params)['data']
    if not isinstance(item, dict):
        raise ValueError('Expected object')
    return item


def snapshot_preview(position, orders, tick, percent, quote_data, provider):
    """Only preview when there are NO working orders for that market/position.
    Does not make a trading call. Refuse ambiguous snapshots.
    """
    pid, mid = position['id'], position['market_id']
    potentially_related = [o for o in orders if o.get('target_position_id') == pid or
                           (o.get('market_id') == mid and o.get('target_position_id') is None)]
    if potentially_related:
        raise ValueError('Working or ambiguous same-market orders present; manual review required')
    price = proposed_stop(position, tick, percent)
    size = dec(position['size'])
    if size <= 0:
        raise ValueError('Invalid position size')
    # Do not preview a stop already breached by the authoritative top of book.
    if provider in ('ostium', 'veranta'):
        current = dec(quote_data['mid'])
    elif position['side'] == 'short':
        current = dec(quote_data['ask'])
        if price <= current:
            raise ValueError('Stop at/below current ask')
    else:
        current = dec(quote_data['bid'])
        if price >= current:
            raise ValueError('Stop at/above current bid')
    return {
        'endpoint': '/v1/positions/' + quote(str(pid), safe='') + '/exit-orders',
        'method': 'PUT (PREVIEW ONLY; NEVER SENT)',
        'body': {
            'expected_position_size': float(size),
            'expected_orders': [],
            'operations': [{'kind': 'place', 'group': 'sl', 'price': float(price),
                            'size': float(size), 'execution_type': 'market'}]
        },
        'warning': 'Snapshot must be rechecked immediately before any future write. No write implemented.'
    }


class Client:
    def __init__(self, url, key):
        self.url = url
        self.session = requests.Session()
        self.session.headers.update({'Authorization': 'Bearer ' + key,
                                     'Accept': 'application/json'})

    def get(self, endpoint, params=None, timeout=15):
        return self.session.get(self.url + endpoint, params=params, timeout=timeout)


def scan(client, percentage):
    accounts = pages(client, '/v1/accounts', {}, paginated=False)
    markets = pages(client, '/v1/markets', {}, paginated=False)
    by_market = {m.get('market_id'): m for m in markets if isinstance(m, dict)}
    print('Accounts:', len(accounts))
    for account in accounts:
        aid = account.get('id')
        if not aid:
            print('Account missing ID; skipping')
            continue
        try:
            positions = pages(client, '/v1/positions', {'account_id': aid, 'status':'open'}, paginated=False)
            orders = pages(client, '/v1/orders', {'account_id': aid, 'status':'working', 'limit':100})
        except (requests.RequestException, ValueError) as exc:
            print('Account scan failed. SL status UNKNOWN:', type(exc).__name__)
            continue
        print('Account ending', str(aid)[-4:], '| positions:',len(positions),'| working orders:',len(orders))
        for p in positions:
            symbol, side = p.get('symbol','?'), p.get('side','?')
            print('  ', symbol, side, '| SL: ', end='')
            try:
                candidates = matching_stop(p, orders, positions)
                if candidates:
                    print('Candidate working stop found (confirm on website); no action')
                    continue
                print('NOT VERIFIED')
                mid = p['market_id']
                market = by_market.get(mid)
                if not market:
                    market = get_single(client, '/v1/markets/' + quote(str(mid), safe=''))
                if market.get('market_id') != mid or not market.get('tick_size'):
                    print('     SKIP: missing or mismatched official market data')
                    continue
                if market.get('trading_enabled') is not True and market.get('reduce_only') is not True:
                    print('     SKIP: market does not allow trading or reduce-only orders')
                    continue
                close_side = 'buy' if side == 'short' else 'sell'
                quote_data = get_single(
                    client,
                    '/v1/markets/'+quote(str(mid), safe='')+'/quote',
                    {'side': close_side, 'size': p['size']}
                )
                if (quote_data.get('status') != 'ok' or quote_data.get('market_id') != mid or
                        quote_data.get('fillable') is not True or quote_data.get('requested_side') != close_side or
                        dec(quote_data.get('requested_size')) != dec(p['size'])):
                    raise ValueError('No valid size-aware close quote')
                preview = snapshot_preview(
                    p, orders, market['tick_size'], percentage, quote_data, market['provider']
                )
                # Never display secret IDs in preview unless local terminal user wants them.
                print('     DRY-RUN ONLY:',json.dumps(preview, separators=(',',':')))
            except (KeyError, TypeError, ValueError, InvalidOperation, requests.RequestException) as exc:
                print('     SKIP preview; manual review:',type(exc).__name__,str(exc)[:130])
    print('READ ONLY — no trade requests sent')


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch', action='store_true', help='Repeat read-only scans')
    args = parser.parse_args()
    if os.getenv('DRY_RUN','').lower() != 'true':
        raise SystemExit('SAFETY LOCK: set DRY_RUN=true')
    url = os.getenv('MFP_BASE_URL','').rstrip('/')
    key = os.getenv('MFP_API_KEY','')
    if url not in (LIVE, SANDBOX):
        raise SystemExit('Unrecognized API URL')
    if not key or (url == LIVE and not key.startswith('fp_live_')) or (url == SANDBOX and not key.startswith('fp_test_')):
        raise SystemExit('Invalid/mismatched key or environment')
    try:
        percentage = dec(os.getenv('SL_PERCENT','1'))
        interval = max(15, int(os.getenv('CHECK_INTERVAL','30')))
        if not (0 < percentage < 100):
            raise ValueError('SL percent out of range')
    except (ValueError, InvalidOperation):
        raise SystemExit('Invalid risk or interval config')
    client = Client(url, key)
    print('MyFundedPerps v3 — READ ONLY — never sends PUT/POST/PATCH/DELETE')
    while True:
        print('\nScan',datetime.now().isoformat(timespec='seconds'))
        try:
            scan(client, percentage)
        except (requests.RequestException, ValueError) as exc:
            print('Scan incomplete; protection UNKNOWN:', type(exc).__name__)
        if not args.watch:
            break
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print('\nStopped')
            break


if __name__ == '__main__':
    main()
