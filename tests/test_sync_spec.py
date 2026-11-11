"""Pure sync validation and portable settings regressions (no cluster or tools)."""
import copy
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hpclib' / 'servers'))
from sync_spec import SyncError, classify, effective_git, validate, validate_metadata


def spec(local='/tmp/sync-fixture'):
    return {'name': 'Code', 'local': local, 'targets': [
        {'cluster': 'grace', 'remote': '/work/me/code'},
        {'cluster': 'entropy', 'remote': '/scratch/me/code'}]}


class TestSyncSpec(unittest.TestCase):
    def test_default_is_git_without_rclone(self):
        out = validate(spec())
        self.assertEqual(out['mode'], 'git')
        self.assertEqual(out['git']['ignored'], 'exclude')
        self.assertNotIn('rclone', out)
        self.assertEqual(out['policy'], {'on_change': 'notify', 'merge': 'undecided'})
        self.assertNotEqual(out['targets'][0]['id'], out['targets'][1]['id'])

    def test_direction_defaults_and_legacy_rclone_are_explicit(self):
        self.assertEqual(validate(spec())['direction'], 'both')
        for direction in ('both', 'push', 'pull'):
            value = spec()
            value['direction'] = direction
            self.assertEqual(validate(value)['direction'], direction)
        value = spec()
        value.update(mode='rclone', rclone={'direction': 'pull'})
        self.assertEqual(validate(value)['direction'], 'pull')
        value['direction'] = 'push'
        with self.assertRaises(SyncError):
            validate(value)
        value = spec()
        value['direction'] = 'sideways'
        with self.assertRaises(SyncError):
            validate(value)

    def test_thresholds_are_configurable_and_finite(self):
        s = spec()
        s['git'] = {'warn_file_mb': 0.5, 'warn_snapshot_mb': 9999, 'warn_file_count': 7}
        self.assertEqual(validate(s)['git']['warn_file_count'], 7)
        for field, bad in [('warn_file_mb', math.inf), ('warn_snapshot_mb', math.nan),
                           ('warn_file_mb', 0), ('warn_snapshot_mb', 10 ** 500),
                           ('warn_file_count', True), ('warn_file_count', 1.5)]:
            with self.subTest(field=field, bad=bad):
                s['git'] = {field: bad}
                with self.assertRaises(SyncError):
                    validate(s)

    def test_reject_invalid_shapes_paths_and_branches(self):
        for local in ('relative', '/', '/tmp/../etc', '/tmp/line\nbreak'):
            with self.subTest(local=local), self.assertRaises(SyncError):
                validate(spec(local))
        for branch in ('-x', 'a..b', 'a.lock', 'a/.hidden', 'a@{b', 'a b', 'a~b', 'a[0]'):
            s = spec()
            s['git'] = {'branch': branch}
            with self.subTest(branch=branch), self.assertRaises(SyncError):
                validate(s)
        for field, value in [('interval', True), ('interval', 1), ('targets', []), ('mode', 'other'), ('unknown', 1)]:
            s = spec()
            s[field] = value
            with self.subTest(field=field), self.assertRaises(SyncError):
                validate(s)

    def test_target_ids_stable_and_duplicates_refused(self):
        a = spec()
        a['targets'][0]['remote'] += '/'
        self.assertEqual(validate(a)['targets'], validate(spec())['targets'])
        a['targets'].append(copy.deepcopy(a['targets'][0]))
        with self.assertRaises(SyncError):
            validate(a)

    def test_metadata_only_portable_known_fields(self):
        m = {'v': 1, 'git': {'default_excludes': 'off', 'default_excludes_version': 1, 'warn_file_mb': 42}}
        self.assertEqual(validate_metadata(m), m)
        for change in ({'token': 'secret'}, {'local': '/tmp/a'}, {'v': True}, {'git': {'branch': 'main'}}):
            bad = dict(m, **change)
            with self.subTest(change=change), self.assertRaises(SyncError):
                validate_metadata(bad)

    def test_override_precedence(self):
        s = validate(spec())
        s['git']['warn_file_mb'] = 80
        m = {'git': {'warn_file_mb': 40, 'warn_file_count': 30}}
        effective, sources = effective_git(s, m, ['warn_file_mb'])
        self.assertEqual(effective['warn_file_mb'], 80)
        self.assertEqual(effective['warn_file_count'], 30)
        self.assertEqual(sources['warn_file_mb'], 'sync setting')
        self.assertEqual(sources['warn_file_count'], 'repository metadata')

    def test_classification_including_equal_initial_trees(self):
        self.assertEqual(classify('a', 'a', None), 'initial-review')
        self.assertEqual(classify('a', 'a', 'b'), 'in-sync')
        self.assertEqual(classify('a', 'b', 'b'), 'local-changed')
        self.assertEqual(classify('a', 'b', 'a'), 'remote-changed')
        self.assertEqual(classify('a', 'b', 'c'), 'both-changed')

    def test_no_unimplemented_option_silently_enables_transfer(self):
        s = spec()
        s['git'] = {'ignored': 'rclone'}
        with self.assertRaises(SyncError) as caught:
            validate(s)
        self.assertEqual(caught.exception.status, 501)
        s = spec()
        s['rclone'] = {'direction': 'push', 'delete': False, 'filter': ['+ /inputs/**', '- **']}
        out = validate(s)
        self.assertEqual(out['git']['ignored'], 'exclude')


if __name__ == '__main__':
    unittest.main()
