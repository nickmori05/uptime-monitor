from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor


class SummaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / 'checks.sqlite3'
        self.connection = monitor.connect(self.database)
        self.addCleanup(self.connection.close)

    def save(self, state='up', latency=10, url='https://example.com', timestamp='2026-09-27T12:00:00+00:00'):
        return monitor.save(self.connection, monitor.Check(
            url, timestamp, state, 200 if state == 'up' else None, latency,
            None if state == 'up' else 'timeout'))

    def test_summary_counts_failures_but_excludes_them_from_success_latency(self):
        self.save(latency=10)
        self.save(latency=30)
        last_id = self.save(state='down', latency=5000)
        row, = monitor.summarize(self.connection)
        self.assertEqual(row, {
            'url': 'https://example.com', 'stored_checks': 3, 'sampled_checks': 3,
            'up_checks': 2, 'down_checks': 1, 'latest_id': last_id,
            'latest_checked_at': '2026-09-27T12:00:00+00:00', 'latest_state': 'down',
            'latest_error': 'timeout', 'latest_status_code': None,
            'up_avg_latency_ms': 20, 'up_min_latency_ms': 10, 'up_max_latency_ms': 30,
        })

    def test_sample_is_per_url_and_uses_record_order_even_if_clock_moves_back(self):
        self.save(latency=999)
        self.save(latency=15, url='https://other.example')
        self.save(latency=20)
        latest = self.save(state='down', timestamp='2026-09-26T12:00:00+00:00')
        rows = monitor.summarize(self.connection, last=2)
        self.assertEqual([row['url'] for row in rows], ['https://example.com', 'https://other.example'])
        self.assertEqual(rows[0]['sampled_checks'], 2)
        self.assertEqual(rows[0]['stored_checks'], 3)
        self.assertEqual(rows[0]['up_avg_latency_ms'], 20)
        self.assertEqual(rows[0]['latest_id'], latest)
        self.assertEqual(rows[0]['latest_checked_at'], '2026-09-26T12:00:00+00:00')
        self.assertEqual(rows[1]['sampled_checks'], 1)

    def test_all_failures_have_null_success_latency(self):
        self.save(state='down')
        row, = monitor.summarize(self.connection)
        self.assertEqual(row['up_checks'], 0)
        self.assertEqual(row['down_checks'], 1)
        for key in ('up_avg_latency_ms', 'up_min_latency_ms', 'up_max_latency_ms'):
            self.assertIsNone(row[key])

    def test_latest_success_does_not_reuse_previous_error(self):
        self.save(state='down')
        self.save(latency=0)
        row, = monitor.summarize(self.connection, last=1)
        self.assertEqual(row['latest_state'], 'up')
        self.assertEqual(row['latest_status_code'], 200)
        self.assertIsNone(row['latest_error'])
        self.assertEqual(row['up_avg_latency_ms'], 0)

    def test_exact_url_filter_and_empty_results(self):
        self.assertEqual(monitor.summarize(self.connection), [])
        url = "https://example.com/search?q=O'Brien"
        self.save(url=url)
        self.save(url='https://example.com/search?q=other')
        self.assertEqual([row['url'] for row in monitor.summarize(self.connection, url=url)], [url])
        self.assertEqual(monitor.summarize(self.connection, url='https://missing.example'), [])

    def test_target_aliases_do_not_duplicate_history_and_removal_keeps_summary(self):
        url = 'https://example.com'
        monitor.add_target(self.connection, 'one', url)
        monitor.add_target(self.connection, 'two', url)
        self.save()
        monitor.remove_target(self.connection, 'one')
        monitor.remove_target(self.connection, 'two')
        row, = monitor.summarize(self.connection)
        self.assertEqual(row['stored_checks'], 1)

    def test_invalid_arguments_are_rejected(self):
        for last in (0, -1, 10001, True, 1.5):
            with self.subTest(last=last), self.assertRaises(ValueError):
                monitor.summarize(self.connection, last=last)
        with self.assertRaises(ValueError):
            monitor.summarize(self.connection, url='file:///etc/hosts')
        with self.assertRaisesRegex(ValueError, 'Summary state'):
            monitor.summarize(self.connection, state='unknown')

    def test_state_filter_uses_latest_result_without_removing_other_samples(self):
        self.save(state='up')
        self.save(state='down')
        self.save(state='down', url='https://recovered.example')
        self.save(state='up', url='https://recovered.example')
        down, = monitor.summarize(self.connection, state='down')
        self.assertEqual(down['url'], 'https://example.com')
        self.assertEqual((down['up_checks'], down['down_checks'], down['sampled_checks']), (1, 1, 2))
        up, = monitor.summarize(self.connection, state='up')
        self.assertEqual(up['url'], 'https://recovered.example')
        self.assertEqual(up['down_checks'], 1)

    def test_cli_state_filter_combines_with_url_and_sample_size(self):
        self.save(state='down')
        self.save(state='up')
        args = ['--database', str(self.database), 'summary', '--url', 'https://example.com', '--last', '1']
        for state in ('up', 'down'):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(monitor.main(args + ['--state', state]), 0)
            rows = json.loads(output.getvalue())
            if state == 'up':
                self.assertEqual(rows[0]['sampled_checks'], 1)
                self.assertEqual(rows[0]['down_checks'], 0)
            else:
                self.assertEqual(rows, [])

    def test_cli_reads_saved_history_without_network_or_writes(self):
        self.save(state='down')
        original = monitor.history(self.connection)
        args = ['--database', str(self.database), 'summary']
        output = io.StringIO()
        with patch('monitor.probe') as probe, redirect_stdout(output):
            self.assertEqual(monitor.main(args + ['--last', '1', '--url', 'https://example.com']), 0)
        probe.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())[0]['down_checks'], 1)
        self.assertEqual(monitor.history(self.connection), original)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(monitor.main(args + ['--last', '0']), 2)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('Summary sample size', errors.getvalue())

    def test_cli_target_uses_current_saved_url_and_combines_with_filters(self):
        monitor.add_target(self.connection, 'api', 'https://old.example')
        self.save(url='https://old.example')
        monitor.update_target(self.connection, 'api', url='https://new.example')
        self.save(url='https://new.example')
        self.save(state='down', url='https://new.example')
        args = ['--database', str(self.database), 'summary', '--target', 'api', '--last', '1', '--state', 'down']
        output = io.StringIO()
        with patch('monitor.probe') as probe, redirect_stdout(output):
            self.assertEqual(monitor.main(args), 0)
        probe.assert_not_called()
        row, = json.loads(output.getvalue())
        self.assertEqual(row['url'], 'https://new.example')
        self.assertEqual((row['sampled_checks'], row['down_checks']), (1, 1))

    def test_cli_missing_target_reports_error_and_url_target_conflict_is_rejected(self):
        args = ['--database', str(self.database), 'summary', '--target', 'missing']
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(monitor.main(args), 2)
        self.assertEqual(output.getvalue(), '')
        self.assertIn("Target 'missing' does not exist", errors.getvalue())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            monitor.main(args + ['--url', 'https://example.com'])
        self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
