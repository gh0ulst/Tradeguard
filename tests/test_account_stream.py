import json
import unittest
from unittest.mock import Mock, patch

from websocket import ABNF, WebSocketBadStatusException

from account_stream import StreamHalt, session_scans, watch_accounts


def frames(*messages):
    socket = Mock()
    socket.getstatus.return_value = 101
    socket.recv_data.side_effect = [(ABNF.OPCODE_TEXT, json.dumps(m)) for m in messages]
    return socket


class AccountStreamTests(unittest.TestCase):
    def test_only_scoped_events_trigger_scans_after_snapshot(self):
        socket = frames({'type': 'snapshot', 'seq': 1, 'account_id': 'A'},
                        {'type': 'snapshot_end'},
                        {'type': 'order', 'seq': 2, 'account_id': 'B'},
                        {'type': 'heartbeat'},
                        {'type': 'position', 'seq': 3, 'account_id': 'A'})
        with patch('account_stream.MIN_EVENT_INTERVAL', 0):
            scans = session_scans(socket, 'A', 30, lambda: False)
            next(scans)
            self.assertEqual(socket.recv_data.call_count, 2)
            next(scans)
            self.assertEqual(socket.recv_data.call_count, 5)

    def test_price_review_on_quiet_active_account(self):
        now = [0]
        socket = frames({'type': 'snapshot', 'seq': 1, 'account_id': 'A'},
                        {'type': 'snapshot_end'}, {'type': 'heartbeat'})
        receive = socket.recv_data.side_effect

        def read(**kwargs):
            frame = next(receive)
            if 'heartbeat' in frame[1]:
                now[0] = 30
            return frame

        socket.recv_data.side_effect = read
        with patch('account_stream.time.monotonic', side_effect=lambda: now[0]):
            scans = session_scans(socket, 'A', 30, lambda: True)
            next(scans)
            next(scans)
            self.assertEqual(now[0], 30)

    def test_scope_loss_halts(self):
        for messages in [
            [{'type': 'snapshot_end'}],
            [{'type': 'snapshot', 'seq': 1, 'account_id': 'A'},
             {'type': 'snapshot_end'},
             {'type': 'account', 'seq': 2, 'account_id': 'A', 'event': 'removed'}],
        ]:
            with self.subTest(messages=messages), self.assertRaises(StreamHalt):
                list(session_scans(frames(*messages), 'A', 30, lambda: False))

    def test_gap_reconnects_and_requires_fresh_snapshot(self):
        first = frames({'type': 'snapshot', 'seq': 1, 'account_id': 'A'},
                       {'type': 'snapshot_end'}, {'type': 'position', 'seq': 3, 'account_id': 'A'})
        second = frames({'type': 'snapshot', 'seq': 1, 'account_id': 'A'}, {'type': 'snapshot_end'})
        with patch('account_stream.create_connection', side_effect=[first, second]) as connect, \
                patch('account_stream.time.sleep') as sleep:
            scans = watch_accounts('fp_test_example', 'A', 30, lambda: False)
            next(scans)
            next(scans)
            self.assertTrue(connect.call_args.args[0].endswith('/sandbox/stream'))
            sleep.assert_called_once()
            first.close.assert_called_once()
            self.assertEqual(second.recv_data.call_count, 2)
            scans.close()

    def test_denied_credentials_halt_before_scan(self):
        error = WebSocketBadStatusException('denied', status_code=401)
        with patch('account_stream.create_connection', side_effect=error), self.assertRaises(StreamHalt):
            next(watch_accounts('fp_live_example', 'A', 30, lambda: False))
