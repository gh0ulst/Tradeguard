"""Controlled SINGLE-ATTEMPT MyFundedPerps stop-loss test. No automatic repeats.
Only run on an expendable competition account that permits API automation.
"""
import argparse
import os
import sys
from decimal import Decimal
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from main import Client, LIVE, pages, get_single, proposed_stop, dec


def fail(message):
    raise SystemExit('STOP: ' + message)


def find_one_position(client, position_id):
    accounts = pages(client, '/v1/accounts', {}, paginated=False)
    matches = []
    for a in accounts:
        if not a.get('id'):
            fail('Account missing ID')
        positions = pages(client, '/v1/positions', {'account_id': a['id'], 'status': 'open'}, paginated=False)
        matches.extend(p for p in positions if p.get('id') == position_id)
    if len(matches) != 1:
        fail('Expected exactly one open position with that ID; found ' + str(len(matches)))
    return matches[0]


def snapshot(client, pid):
    p = find_one_position(client, pid)
    aid, mid = p['account_id'], p['market_id']
    # Read FULL working set; any related/ambiguous order is a stop condition.
    orders = pages(client, '/v1/orders', {'account_id': aid, 'status': 'working', 'limit': 100})
    related = [o for o in orders if o.get('target_position_id') == pid or o.get('market_id') == mid]
    if related:
        fail('Working orders exist on target position or market. Manual review required; will not create a possible duplicate.')
    market = get_single(client, '/v1/markets/' + quote(str(mid), safe=''))
    if market.get('market_id') != mid or market.get('trading_enabled') is False:
        fail('Missing/mismatched market or trading disabled')
    tick, step = dec(market['tick_size']), dec(market['size_step'])
    size = dec(p['size'])
    if tick <= 0 or step <= 0 or size <= 0 or size % step != 0:
        fail('Invalid size or market increments')
    price = proposed_stop(p, tick, Decimal('1'))
    q = get_single(client, '/v1/markets/' + quote(str(mid), safe='') + '/quote')
    if q.get('status') != 'ok' or q.get('market_id') != mid:
        fail('Market quote invalid')
    # A quote with null bid/ask is not sufficient for an automated test.
    reference = dec(q['ask'] if p['side'] == 'short' else q['bid'])
    if reference <= 0 or (p['side'] == 'short' and price <= reference) or (p['side'] == 'long' and price >= reference):
        fail('Proposed stop is already crossed or wrong side of market')
    return p, price, {
        'expected_position_size': float(size),
        'expected_orders': [],
        'operations': [{'kind': 'place', 'group': 'sl', 'price': float(price), 'size': float(size), 'execution_type': 'market'}]
    }


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description='One-off competition-account stop-loss test')
    parser.add_argument('--position-id', required=True, help='Full position ID from your local terminal. Never share it.')
    parser.add_argument('--execute', action='store_true', help='Allow one explicitly confirmed PUT request')
    args = parser.parse_args()
    base = os.getenv('MFP_BASE_URL', '').rstrip('/')
    key = os.getenv('MFP_API_KEY', '')
    if base != LIVE or not key.startswith('fp_live_'):
        fail('This script expects your existing production API and a live-format key')
    if os.getenv('DRY_RUN', '').lower() != 'true':
        fail('Keep DRY_RUN=true as a second safety guard; --execute is separately required')
    client = Client(base, key)
    try:
        p, price, body = snapshot(client, args.position_id)
        print('Target:', p['symbol'], p['side'], '| entry:', p['entry_price'], '| size:', p['size'])
        print('Proposed 1% stop:', price, '| market:', p['market_id'])
        print('Snapshot: no working orders for this position or market; price increment validated.')
        print('Proposed SL placement payload:', body)
        if not args.execute:
            print('PREVIEW ONLY. No requests were sent that change orders.')
            return
        print('This is a production API write. It may affect competition account results or eligibility.')
        if input('Type PLACE STOP to authorize exactly one order attempt: ').strip() != 'PLACE STOP':
            fail('Confirmation not received')
        # Recheck account, orders and current quote immediately prior to submission.
        latest, new_price, new_body = snapshot(client, args.position_id)
        if (new_body != body or latest['size'] != p['size'] or latest['entry_price'] != p['entry_price']
                or latest['side'] != p['side'] or latest['market_id'] != p['market_id']):
            fail('Snapshot changed; rerun review')
        endpoint = '/v1/positions/' + quote(str(p['id']), safe='') + '/exit-orders'
        # No automatic retries. API documents no Idempotency-Key for this PUT operation.
        try:
            response = client.session.put(base + endpoint, json=body, timeout=15)
        except requests.RequestException:
            fail('Write outcome UNKNOWN. Check website and orders before any further attempts; do NOT retry blindly.')
        print('Submission HTTP status:', response.status_code)
        if response.status_code != 200:
            fail('Order not confirmed. Check account manually before another attempt. X-Request-Id=' + str(response.headers.get('X-Request-Id', 'unavailable')))
        print('API accepted update. Verifying working orders now...')
        try:
            orders = pages(client, '/v1/orders', {'account_id': p['account_id'], 'status': 'working', 'limit': 100})
            matches = [o for o in orders if o.get('target_position_id') == p['id']
                       and o.get('type') == 'stop_market' and o.get('reduce_only') is True
                       and o.get('side') == ('buy' if p['side'] == 'short' else 'sell')
                       and dec(o.get('size', 0)) >= dec(p['size'])
                       and dec(o.get('trigger_price', 0)) == new_price
                       and o.get('status') in ('resting', 'pending')]
        except (requests.RequestException, ValueError, KeyError):
            fail('Post-write check failed: status UNKNOWN; manually inspect account immediately')
        if len(matches) == 1:
            print('ONE candidate protective stop confirmed in working orders. Verify on trading website.')
        else:
            fail('Cannot verify exactly one matching working SL. Inspect website immediately; do not retry.')
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        fail('API/validation error (' + type(e).__name__ + '). No automatic retry; check website.')

if __name__ == '__main__':
    main()
