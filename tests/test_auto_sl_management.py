import json
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import auto_sl
from main import matching_stop


class StopManagementTests(unittest.TestCase):
    def setUp(self):
        self.position = {
            'id': 'position-1',
            'account_id': 'account-1',
            'market_id': 'binance|TEST',
            'side': 'long',
            'size': 1,
            'entry_price': 100,
            'status': 'open',
        }
        self.stop = {
            'id': 'stop-1',
            'account_id': 'account-1',
            'market_id': 'binance|TEST',
            'side': 'sell',
            'reduce_only': True,
            'status': 'resting',
            'type': 'stop_market',
            'trigger_price': 90,
            'size': 1,
        }

    def test_breakeven_moves_long_stop_to_entry(self):
        state = {'entry_price': '100', 'risk': '10', 'best_price': '110'}
        self.assertEqual(
            auto_sl.next_managed_stop(
                self.position, self.stop, state, Decimal('110'),
                Decimal('1'), Decimal('2'), Decimal('1'), Decimal('0.5')
            ),
            Decimal('100'),
        )

    def test_trailing_stop_uses_best_price_and_rounds_down_for_long(self):
        state = {'entry_price': '100', 'risk': '10', 'best_price': '125.2'}
        self.assertEqual(
            auto_sl.next_managed_stop(
                self.position, self.stop, state, Decimal('125.2'),
                Decimal('1'), Decimal('2'), Decimal('1'), Decimal('0.5')
            ),
            Decimal('115.0'),
        )

    def test_trailing_stop_tightens_short_and_never_loosens(self):
        position = dict(self.position, side='short')
        stop = dict(self.stop, side='buy', trigger_price=110)
        state = {'entry_price': '100', 'risk': '10', 'best_price': '75'}
        self.assertEqual(
            auto_sl.next_managed_stop(
                position, stop, state, Decimal('75'),
                Decimal('1'), Decimal('2'), Decimal('1'), Decimal('0.5')
            ),
            Decimal('85'),
        )
        tighter = dict(stop, trigger_price=85)
        self.assertIsNone(
            auto_sl.next_managed_stop(
                position, tighter, state, Decimal('75'),
                Decimal('1'), Decimal('2'), Decimal('1'), Decimal('0.5')
            )
        )

    def test_breakeven_stop_is_recognized(self):
        stop = dict(self.stop, trigger_price=100)
        self.assertEqual(matching_stop(self.position, [stop], [self.position]), [stop])

    def test_oversized_stop_is_not_matched_after_position_reduction(self):
        reduced = dict(self.position, size=0.5)
        self.assertEqual(matching_stop(reduced, [self.stop], [reduced]), [])

    def test_unknown_position_side_is_rejected(self):
        position = dict(self.position, side='unknown')
        with self.assertRaisesRegex(ValueError, 'Unknown position side'):
            matching_stop(position, [self.stop], [position])

    def test_ambiguous_same_market_orders_cause_no_write(self):
        class Client:
            def __init__(self):
                self.session = self
                self.calls = []

            def put(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        client = Client()
        auto_sl.manage_position_stop(
            client, self.position, self.stop,
            [self.stop, dict(self.stop, id='other-order')],
            {'breakeven_r': Decimal('1'), 'activation_r': Decimal('2'),
             'distance_r': Decimal('1')},
            True, set()
        )
        self.assertFalse(client.calls)

    def test_unresolved_stop_update_must_match_before_management_resumes(self):
        state = {
            'position-1': {
                'entry_price': '100',
                'side': 'long',
                'market_id': self.position['market_id'],
                'initial_stop': '90',
                'risk': '10',
                'best_price': '110',
                'pending_stop': '100',
            }
        }
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(auto_sl, 'STATE', Path(directory) / 'state.json'):
            with self.assertRaisesRegex(RuntimeError, 'outcome is uncertain'):
                auto_sl.management_state(
                    self.position, self.stop, Decimal('110'), state
                )

            confirmed = dict(self.stop, trigger_price=100)
            managed = auto_sl.management_state(
                self.position, confirmed, Decimal('110'), state
            )
            self.assertIsNone(managed['pending_stop'])

    def test_live_update_uses_stale_snapshot_guard_and_verifies(self):
        class Response:
            status_code = 200

        class Session:
            def __init__(self):
                self.calls = []

            def put(self, url, json, timeout):
                self.calls.append((url, json, timeout))
                return Response()

        class Client:
            url = 'https://api.example'

            def __init__(self):
                self.session = Session()

        client = Client()
        config = {
            'breakeven_r': Decimal('1'),
            'activation_r': Decimal('2'),
            'distance_r': Decimal('1'),
        }
        market = {
            'market_id': self.position['market_id'],
            'provider': 'binance',
            'tick_size': '0.5',
        }
        quote_data = {
            'status': 'ok',
            'market_id': self.position['market_id'],
            'fillable': True,
            'requested_side': 'sell',
            'requested_size': '1',
            'bid': '125',
            'ask': '125.5',
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / 'state.json'
            with patch.object(auto_sl, 'STATE', state_path), \
                    patch.object(auto_sl, 'JOURNAL', Path(directory) / 'journal.json'), \
                    patch.object(auto_sl, 'ACCOUNT_ID', 'account-1', create=True), \
                    patch.object(auto_sl, 'get_single', side_effect=[market, quote_data, quote_data]), \
                    patch.object(auto_sl, 'account_snapshot', return_value=(
                        [self.position], [self.stop]
                    )), \
                    patch.object(auto_sl, 'validate_after', return_value=self.stop):
                attempted = set()
                auto_sl.manage_position_stop(
                    client, self.position, self.stop, [self.stop],
                    config, True, attempted
                )

            self.assertEqual(len(client.session.calls), 1)
            url, body, timeout = client.session.calls[0]
            self.assertTrue(url.endswith('/v1/positions/position-1/exit-orders'))
            self.assertEqual(body, {
                'expected_position_size': 1.0,
                'expected_orders': [{
                    'order_id': 'stop-1',
                    'price': 90.0,
                    'size': 1.0,
                    'execution_type': 'market',
                }],
                'operations': [{
                    'kind': 'modify',
                    'order_id': 'stop-1',
                    'price': 115.0,
                }],
            })
            self.assertEqual(timeout, 15)
            self.assertFalse(attempted)
            saved = json.loads(state_path.read_text())['position-1']
            self.assertEqual(saved['best_price'], '125')
            self.assertIsNone(saved['pending_stop'])

    def _run_management_with_mocks(
            self, *, quotes=None, fresh_position=None, fresh_stop=None,
            response_status=200, request_error=None, notifier=None
    ):
            class Response:
                def __init__(self, status_code):
                    self.status_code = status_code
                    self.headers = {'X-Request-Id': 'request-1'}

            class Session:
                def __init__(self):
                    self.calls = []

                def put(self, url, json, timeout):
                    self.calls.append((url, json, timeout))
                    if request_error is not None:
                        raise request_error
                    return Response(response_status)

            class Client:
                url = 'https://api.example'

                def __init__(self):
                    self.session = Session()

            market = {
                'market_id': self.position['market_id'],
                'provider': 'binance',
                'tick_size': '0.5',
            }
            quote_data = {
                'status': 'ok',
                'market_id': self.position['market_id'],
                'fillable': True,
                'requested_side': 'sell',
                'requested_size': '1',
                'bid': '125',
                'ask': '125.5',
            }
            quote_results = [quote_data, quote_data] if quotes is None else quotes
            updated_position = self.position if fresh_position is None else fresh_position
            updated_stop = self.stop if fresh_stop is None else fresh_stop
            client = Client()
            config = {
                'breakeven_r': Decimal('1'),
                'activation_r': Decimal('2'),
                'distance_r': Decimal('1'),
            }

            notifier = notifier or auto_sl.AlertNotifier()
            with tempfile.TemporaryDirectory() as directory:
                state_path = Path(directory) / 'state.json'
                with patch.object(auto_sl, 'STATE', state_path), \
                        patch.object(auto_sl, 'JOURNAL', Path(directory) / 'journal.json'), \
                        patch.object(auto_sl, 'ACCOUNT_ID', 'account-1', create=True), \
                        patch.object(
                            auto_sl, 'NOTIFIER', notifier
                        ), \
                        patch.object(
                            auto_sl, 'get_single',
                            side_effect=[market, *quote_results]
                        ), \
                        patch.object(auto_sl, 'account_snapshot', return_value=(
                            [updated_position], [updated_stop]
                        )), \
                        patch.object(auto_sl, 'validate_after', return_value=updated_stop):
                    attempted = set()
                    error = None
                    notifier.start()
                    try:
                        auto_sl.manage_position_stop(
                            client, self.position, self.stop, [self.stop],
                            config, True, attempted
                        )
                    except RuntimeError as exc:
                        error = exc
                    notifier.pending.join()
                    notifier.shutdown()
                saved_state = json.loads(state_path.read_text())['position-1']

            return error, client.session.calls, attempted, saved_state

    def test_failed_stop_update_keeps_pending_state_and_does_not_retry(self):
            notifier = auto_sl.AlertNotifier('https://discord.example/webhook-secret')
            response = type('Response', (), {'status_code': 204})()
            with patch('auto_sl.requests.post', return_value=response) as post:
                error, calls, attempted, state = self._run_management_with_mocks(
                    response_status=503, notifier=notifier
                )

            self.assertRegex(str(error), 'STOP UPDATE NOT CONFIRMED')
            self.assertEqual(len(calls), 1)
            self.assertIn('manage-stop:position-1', attempted)
            self.assertEqual(Decimal(state['pending_stop']), Decimal('115'))
            self.assertEqual(post.call_count, 1)
            self.assertIn('Stop update failed', post.call_args.kwargs['json']['content'])

    def test_uncertain_stop_update_sends_critical_alert(self):
            import requests

            notifier = auto_sl.AlertNotifier('https://discord.example/webhook-secret')
            response = type('Response', (), {'status_code': 204})()
            with patch('auto_sl.requests.post', return_value=response) as post:
                error, _, _, _ = self._run_management_with_mocks(
                    request_error=requests.Timeout('interrupted'), notifier=notifier
                )

            self.assertRegex(str(error), 'STOP UPDATE OUTCOME UNKNOWN')
            self.assertEqual(post.call_count, 1)
            self.assertIn('outcome is uncertain', post.call_args.kwargs['json']['content'])

    def test_confirmed_trailing_update_sends_informational_alert(self):
            notifier = auto_sl.AlertNotifier('https://discord.example/webhook-secret')
            response = type('Response', (), {'status_code': 204})()
            with patch('auto_sl.requests.post', return_value=response) as post:
                error, _, _, _ = self._run_management_with_mocks(notifier=notifier)

            self.assertIsNone(error)
            self.assertEqual(post.call_count, 1)
            self.assertIn(
                'Trailing Stop Update Confirmed',
                post.call_args.kwargs['json']['content'],
            )

    def test_confirmed_breakeven_update_sends_informational_alert(self):
            notifier = auto_sl.AlertNotifier('https://discord.example/webhook-secret')
            response = type('Response', (), {'status_code': 204})()
            quote_data = {
                'status': 'ok',
                'market_id': self.position['market_id'],
                'fillable': True,
                'requested_side': 'sell',
                'requested_size': '1',
                'bid': '110',
                'ask': '110.5',
            }
            with patch('auto_sl.requests.post', return_value=response) as post:
                error, _, _, _ = self._run_management_with_mocks(
                    quotes=[quote_data, quote_data], notifier=notifier
                )

            self.assertIsNone(error)
            self.assertEqual(post.call_count, 1)
            self.assertIn(
                'Breakeven Stop Update Confirmed',
                post.call_args.kwargs['json']['content'],
            )

    def test_interrupted_stop_update_keeps_pending_state_and_does_not_retry(self):
            import requests

            error, calls, attempted, state = self._run_management_with_mocks(
                request_error=requests.Timeout('request interrupted')
            )

            self.assertRegex(str(error), 'STOP UPDATE OUTCOME UNKNOWN')
            self.assertEqual(len(calls), 1)
            self.assertIn('manage-stop:position-1', attempted)
            self.assertEqual(Decimal(state['pending_stop']), Decimal('115'))

    def test_rapid_reversal_past_target_prevents_stop_update(self):
            quote_data = {
                'status': 'ok',
                'market_id': self.position['market_id'],
                'fillable': True,
                'requested_side': 'sell',
                'requested_size': '1',
                'bid': '114',
                'ask': '114.5',
            }
            error, calls, attempted, state = self._run_management_with_mocks(
                quotes=[
                    {
                        'status': 'ok',
                        'market_id': self.position['market_id'],
                        'fillable': True,
                        'requested_side': 'sell',
                        'requested_size': '1',
                        'bid': '125',
                        'ask': '125.5',
                    },
                    quote_data,
                ]
            )

            self.assertRegex(str(error), 'target crossed before modification')
            self.assertFalse(calls)
            self.assertFalse(attempted)
            self.assertIsNone(state['pending_stop'])

    def test_position_size_change_prevents_stop_update(self):
            reduced_position = dict(self.position, size=0.5)
            error, calls, attempted, state = self._run_management_with_mocks(
                fresh_position=reduced_position
            )

            self.assertRegex(str(error), 'Position or stop changed immediately')
            self.assertFalse(calls)
            self.assertFalse(attempted)
            self.assertIsNone(state['pending_stop'])

    def test_position_closure_prevents_stop_update(self):
            closed_position = dict(self.position, status='closed')
            error, calls, attempted, state = self._run_management_with_mocks(
                fresh_position=closed_position
            )

            self.assertRegex(str(error), 'Position or stop changed immediately')
            self.assertFalse(calls)
            self.assertFalse(attempted)
            self.assertIsNone(state['pending_stop'])

    def test_missing_and_mismatched_stops_send_critical_alerts(self):
            position = dict(self.position, size=0.5)
            oversized_stop = dict(self.stop, size=1)
            notifier = auto_sl.AlertNotifier('https://discord.example/webhook-secret')
            response = type('Response', (), {'status_code': 204})()
            with patch.object(auto_sl, 'ACCOUNT_ID', 'account-identifier-1234', create=True), \
                    patch.object(auto_sl, 'account_snapshot', return_value=(
                        [position], [oversized_stop]
                    )), \
                    patch.object(
                        auto_sl, 'candidate',
                        return_value=(None, None, 'Working SL candidate already exists')
                    ), \
                    patch.object(auto_sl, 'NOTIFIER', notifier), \
                    patch('auto_sl.requests.post', return_value=response) as post:
                notifier.start()
                auto_sl.process_position(
                    object(), position, Decimal('1'), {}, True, set()
                )
                notifier.pending.join()
                notifier.shutdown()

            self.assertEqual(post.call_count, 2)
            contents = [call.kwargs['json']['content'] for call in post.call_args_list]
            self.assertTrue(any('Protective stop size does not match position' in x for x in contents))
            self.assertTrue(any('no uniquely verified protective stop' in x for x in contents))
            self.assertTrue(all('****1234' in x for x in contents))
            self.assertTrue(all('account-identifier-1234' not in x for x in contents))


class DiscordNotifierTests(unittest.TestCase):
    def setUp(self):
            self.webhook = 'https://discord.example/api/webhooks/webhook-secret'
            self.position = {
                'id': 'position-1',
                'account_id': 'account-secret-9876',
                'market_id': 'binance|TEST',
                'symbol': 'TEST',
                'side': 'long',
            }

    def test_successful_delivery_masks_account_and_does_not_expose_webhook(self):
            notifier = auto_sl.AlertNotifier(self.webhook)
            response = type('Response', (), {'status_code': 204})()
            output = StringIO()
            with patch('auto_sl.requests.post', return_value=response) as post, \
                    patch('sys.stdout', output):
                notifier.start()
                self.assertTrue(notifier.alert(
                    'critical:test', 'CRITICAL', 'Protective stop missing',
                    self.position, details='No verified stop',
                ))
                notifier.pending.join()
                notifier.shutdown()

            post.assert_called_once()
            self.assertEqual(post.call_args.args[0], self.webhook)
            self.assertEqual(post.call_args.kwargs['timeout'], auto_sl.DISCORD_TIMEOUT)
            content = post.call_args.kwargs['json']['content']
            self.assertIn('****9876', content)
            self.assertIn('Market: TEST', content)
            self.assertIn('Side: long', content)
            self.assertNotIn('account-secret-9876', content)
            self.assertNotIn(self.webhook, content)
            self.assertNotIn('api_key', content.lower())
            self.assertNotIn(self.webhook, output.getvalue())

    def test_webhook_exceptions_and_http_errors_do_not_escape(self):
            import requests

            notifier = auto_sl.AlertNotifier(self.webhook)
            output = StringIO()
            with patch('auto_sl.requests.post', side_effect=requests.Timeout(self.webhook)), \
                    patch('sys.stdout', output):
                notifier.start()
                self.assertTrue(notifier.alert(
                    'failure:network', 'CRITICAL', 'Stop update failed', self.position
                ))
                notifier.pending.join()
                notifier.shutdown()
            self.assertNotIn(self.webhook, output.getvalue())

            notifier.resolve('failure:network')
            response = type('Response', (), {'status_code': 500})()
            with patch('auto_sl.requests.post', return_value=response), \
                    patch('sys.stdout', output):
                notifier.start()
                self.assertTrue(notifier.alert(
                    'failure:http', 'CRITICAL', 'Stop update failed', self.position
                ))
                notifier.pending.join()
                notifier.shutdown()

    def test_duplicate_suppression_and_recovery(self):
            notifier = auto_sl.AlertNotifier(self.webhook, suppression_seconds=900)
            response = type('Response', (), {'status_code': 204})()
            with patch('auto_sl.requests.post', return_value=response) as post, \
                    patch('auto_sl.time.monotonic', side_effect=[100, 101, 102]):
                notifier.start()
                self.assertTrue(notifier.alert(
                    'missing-stop:position-1', 'CRITICAL', 'Protective stop missing',
                    self.position,
                ))
                self.assertFalse(notifier.alert(
                    'missing-stop:position-1', 'CRITICAL', 'Protective stop missing',
                    self.position,
                ))
                notifier.resolve('missing-stop:position-1')
                self.assertTrue(notifier.alert(
                    'missing-stop:position-1', 'CRITICAL', 'Protective stop missing',
                    self.position,
                ))
                notifier.pending.join()
                notifier.shutdown()

            self.assertEqual(post.call_count, 2)

    def test_repeated_api_timeouts_alert_once_and_recover(self):
            notifier = auto_sl.AlertNotifier(self.webhook)
            response = type('Response', (), {'status_code': 204})()
            with patch('auto_sl.requests.post', return_value=response) as post:
                notifier.start()
                notifier.record_api_timeout('account-secret-9876', self.position)
                notifier.record_api_timeout('account-secret-9876', self.position)
                self.assertEqual(post.call_count, 0)
                notifier.record_api_timeout('account-secret-9876', self.position)
                notifier.pending.join()
                self.assertEqual(post.call_count, 1)

                notifier.resolve_api_timeouts()
                self.assertEqual(notifier.timeout_count, 0)
                for _ in range(auto_sl.REPEATED_TIMEOUT_THRESHOLD):
                    notifier.record_api_timeout('account-secret-9876', self.position)
                notifier.pending.join()
                notifier.shutdown()

            self.assertEqual(post.call_count, 2)

    def test_unknown_position_side_sends_critical_alert(self):
            notifier = auto_sl.AlertNotifier(self.webhook)
            position = dict(self.position, side='sideways')
            response = type('Response', (), {'status_code': 204})()
            with patch.object(auto_sl, 'ACCOUNT_ID', 'account-secret-9876', create=True), \
                    patch.object(auto_sl, 'account_snapshot', return_value=(
                        [position], []
                    )), \
                    patch.object(auto_sl, 'NOTIFIER', notifier), \
                    patch('auto_sl.requests.post', return_value=response) as post:
                notifier.start()
                with self.assertRaisesRegex(ValueError, 'Unknown position side'):
                    auto_sl.process_position(
                        object(), position, Decimal('1'), {}, True, set()
                    )
                notifier.pending.join()
                notifier.shutdown()

            self.assertEqual(post.call_count, 1)
            self.assertIn(
                'Unknown position side encountered',
                post.call_args.kwargs['json']['content'],
            )

    def test_api_http_failures_alert_after_threshold_and_recover(self):
            import requests

            notifier = auto_sl.AlertNotifier(self.webhook)
            response = type('Response', (), {'status_code': 204})()
            error_response = type('ErrorResponse', (), {'status_code': 503})()
            error = requests.HTTPError(response=error_response)
            with patch('auto_sl.requests.post', return_value=response) as post:
                notifier.start()
                notifier.record_api_failure(error, 'account-secret-9876', self.position)
                notifier.record_api_failure(error, 'account-secret-9876', self.position)
                self.assertEqual(post.call_count, 0)
                notifier.record_api_failure(error, 'account-secret-9876', self.position)
                notifier.pending.join()
                self.assertEqual(post.call_count, 1)
                content = post.call_args.kwargs['json']['content']
                self.assertIn('HTTP 503', content)
                self.assertIn('****9876', content)
                self.assertNotIn('account-secret-9876', content)

                notifier.resolve_api_errors()
                for _ in range(auto_sl.REPEATED_API_FAILURE_THRESHOLD):
                    notifier.record_api_failure(error, 'account-secret-9876', self.position)
                notifier.pending.join()
                notifier.shutdown()

            self.assertEqual(post.call_count, 2)

    def test_safety_halt_sends_critical_alert(self):
            notifier = auto_sl.AlertNotifier(self.webhook)
            response = type('Response', (), {'status_code': 204})()
            with patch.object(auto_sl, 'ACCOUNT_ID', 'account-secret-9876', create=True), \
                    patch.object(auto_sl, 'NOTIFIER', notifier), \
                    patch('auto_sl.requests.post', return_value=response) as post:
                notifier.start()
                auto_sl.alert_safety_halt(
                    'safety-halt:position-1', self.position,
                    'Manual verification required',
                )
                notifier.pending.join()
                notifier.shutdown()

            self.assertEqual(post.call_count, 1)
            self.assertIn('Watcher safety halt', post.call_args.kwargs['json']['content'])
            self.assertNotIn('account-secret-9876', post.call_args.kwargs['json']['content'])

    def test_notifications_enqueue_without_blocking_management_thread(self):
            notifier = auto_sl.AlertNotifier(self.webhook)
            entered = threading.Event()
            release = threading.Event()
            request_threads = []

            def slow_post(*args, **kwargs):
                request_threads.append(threading.get_ident())
                entered.set()
                release.wait(timeout=2)
                return type('Response', (), {'status_code': 204})()

            caller_thread = threading.get_ident()
            with patch('auto_sl.requests.post', side_effect=slow_post):
                notifier.start()
                started = time.perf_counter()
                accepted = notifier.alert('slow:endpoint', 'CRITICAL', 'Test', self.position)
                elapsed = time.perf_counter() - started
                self.assertTrue(accepted)
                self.assertLess(elapsed, 0.1)
                self.assertTrue(entered.wait(timeout=1))
                self.assertNotEqual(request_threads[0], caller_thread)
                release.set()
                notifier.pending.join()
                notifier.shutdown()

    def test_full_queue_drops_alert_without_blocking_or_exposing_secret(self):
            notifier = auto_sl.AlertNotifier(self.webhook, queue_capacity=1)
            output = StringIO()
            entered = threading.Event()
            release = threading.Event()

            def slow_post(*args, **kwargs):
                entered.set()
                release.wait(timeout=2)
                return type('Response', (), {'status_code': 204})()

            with patch('auto_sl.requests.post', side_effect=slow_post), \
                    patch('sys.stdout', output):
                notifier.start()
                self.assertTrue(notifier.alert('queued:1', 'CRITICAL', 'First', self.position))
                self.assertTrue(entered.wait(timeout=1))
                self.assertTrue(notifier.alert('queued:2', 'CRITICAL', 'Second', self.position))
                started = time.perf_counter()
                self.assertFalse(notifier.alert('queued:3', 'CRITICAL', 'Third', self.position))
                elapsed = time.perf_counter() - started
                release.set()
                notifier.pending.join()
                notifier.shutdown()
            self.assertLess(elapsed, 0.1)
            self.assertIn('delivery queue full', output.getvalue())
            self.assertNotIn(self.webhook, output.getvalue())

    def test_worker_shutdown_is_bounded_and_drains_queued_work(self):
            notifier = auto_sl.AlertNotifier(self.webhook)
            entered = threading.Event()
            release = threading.Event()

            def slow_post(*args, **kwargs):
                entered.set()
                release.wait(timeout=2)
                return type('Response', (), {'status_code': 204})()

            with patch('auto_sl.requests.post', side_effect=slow_post):
                notifier.start()
                notifier.alert('shutdown:test', 'INFO', 'Test', self.position)
                self.assertTrue(entered.wait(timeout=1))
                started = time.perf_counter()
                notifier.shutdown(timeout=0.01)
                self.assertLess(time.perf_counter() - started, 0.1)
                release.set()
                notifier.worker.join(timeout=1)
            self.assertFalse(notifier.worker.is_alive())


if __name__ == '__main__':
    unittest.main()
