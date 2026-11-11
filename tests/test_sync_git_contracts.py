"""Stage-0 contracts using real Git only, in disposable repositories.

Set HPCLIB_SYNC_FIXTURE_ROOT for a cluster filesystem. These tests never reach a real
remote, use the user's Git configuration, or modify a source checkout.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('git'), 'Git unavailable')
class TestSyncGitContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hpclib-sync-git-', dir=os.environ.get('HPCLIB_SYNC_FIXTURE_ROOT'))
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.index = self.base / 'sync.index'
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_OPTIONAL_LOCKS='0',
                        GIT_AUTHOR_NAME='Sync fixture', GIT_AUTHOR_EMAIL='sync-fixture@example.invalid',
                        GIT_COMMITTER_NAME='Sync fixture', GIT_COMMITTER_EMAIL='sync-fixture@example.invalid')
        self.git('init', '-q')
        (self.repo / 'keep.txt').write_text('base\n')
        (self.repo / 'gone.txt').write_text('remove later\n')
        (self.repo / '.gitignore').write_text('ignored/\n')
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture base')

    def git(self, *args, private=False, check=True, data=None):
        env = dict(self.env)
        if private:
            env.update(GIT_DIR=str(self.repo / '.git'), GIT_WORK_TREE=str(self.repo), GIT_INDEX_FILE=str(self.index))
        result = subprocess.run(['git', '-c', 'core.excludesFile=' + os.devnull, '-C', str(self.repo), *args],
                                env=env, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if check and result.returncode:
            self.fail('git %s: %s' % (' '.join(args), result.stderr.decode(errors='replace')))
        return result

    def text(self, *args, **kwargs):
        return self.git(*args, **kwargs).stdout.decode().strip()

    def files(self):
        return {str(p.relative_to(self.repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.repo.rglob('*') if p.is_file() and '.git' not in p.relative_to(self.repo).parts}

    def snapshot(self):
        if not self.index.exists():
            self.git('read-tree', 'HEAD', private=True)
        self.git('add', '-A', private=True)
        tree = self.text('write-tree', private=True)
        tip = self.text('rev-parse', '--verify', 'refs/heads/sync', check=False)
        if tip and self.text('rev-parse', tip + '^{tree}') == tree:
            return tree, tip
        parents = ['-p', tip] if tip else ['-p', self.text('rev-parse', 'HEAD')]
        commit = self.text('commit-tree', tree, *parents, data=b'sync snapshot\n')
        self.git('update-ref', 'refs/heads/sync', commit, tip or '0' * 40)
        return tree, commit

    def candidate(self, files, parents=()):
        self.git('read-tree', '--empty', private=True)
        for path, content in files.items():
            blob = self.text('hash-object', '-w', '--stdin', data=content.encode())
            self.git('update-index', '--add', '--cacheinfo', '100644', blob, path, private=True)
        tree = self.text('write-tree', private=True)
        args = ['commit-tree', tree]
        for parent in parents:
            args.extend(['-p', parent])
        return self.text(*args, data=b'fixture commit\n')

    def test_snapshot_preserves_head_staged_index_and_working_content(self):
        (self.repo / 'keep.txt').write_text('staged user edit\n')
        self.git('add', 'keep.txt')
        (self.repo / 'keep.txt').write_text('later working edit\n')
        (self.repo / 'new.txt').write_text('untracked user file\n')
        (self.repo / 'ignored').mkdir()
        (self.repo / 'ignored' / 'large.dat').write_bytes(b'x' * 10000)
        before = (self.text('rev-parse', 'HEAD'), (self.repo / '.git/index').read_bytes(), self.files())
        tree, _ = self.snapshot()
        self.assertEqual(before, (self.text('rev-parse', 'HEAD'), (self.repo / '.git/index').read_bytes(), self.files()))
        listing = self.text('ls-tree', '-r', '--name-only', tree)
        self.assertIn('new.txt', listing)
        self.assertNotIn('large.dat', listing)
        self.assertEqual(self.snapshot()[0], tree)
        self.assertEqual(self.text('rev-list', '--count', 'HEAD..refs/heads/sync'), '1')

    def test_read_tree_applies_edit_and_deletion_through_private_index(self):
        old = self.text('rev-parse', 'HEAD')
        before_index = (self.repo / '.git/index').read_bytes()
        new = self.candidate({'keep.txt': 'incoming\n', '.gitignore': 'ignored/\n'}, [old])
        self.git('read-tree', old, private=True)
        self.git('update-index', '--refresh', private=True)
        self.git('read-tree', '-n', '-m', '-u', old, new, private=True)
        self.assertTrue((self.repo / 'gone.txt').exists())
        self.git('read-tree', '-m', '-u', old, new, private=True)
        self.assertFalse((self.repo / 'gone.txt').exists())
        self.assertEqual((self.repo / 'keep.txt').read_text(), 'incoming\n')
        self.assertEqual(self.text('rev-parse', 'HEAD'), old)
        self.assertEqual((self.repo / '.git/index').read_bytes(), before_index)

    def test_read_tree_refuses_later_working_edit(self):
        old = self.text('rev-parse', 'HEAD')
        new = self.candidate({'keep.txt': 'incoming\n', 'gone.txt': 'remove later\n', '.gitignore': 'ignored/\n'}, [old])
        self.git('read-tree', old, private=True)
        self.git('update-index', '--refresh', private=True)
        (self.repo / 'keep.txt').write_text('later user edit\n')
        before = self.files()
        result = self.git('read-tree', '-m', '-u', old, new, private=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.files(), before)

    def test_read_tree_refuses_differing_untracked_collision(self):
        old = self.text('rev-parse', 'HEAD')
        new = self.candidate({'keep.txt': 'base\n', 'gone.txt': 'remove later\n', '.gitignore': 'ignored/\n', 'new.txt': 'incoming\n'}, [old])
        self.git('read-tree', old, private=True)
        self.git('update-index', '--refresh', private=True)
        (self.repo / 'new.txt').write_text('untracked user content\n')
        before = self.files()
        self.assertNotEqual(self.git('read-tree', '-m', '-u', old, new, private=True, check=False).returncode, 0)
        self.assertEqual(self.files(), before)

    def test_explicit_base_merge_uses_git_status_and_preserves_worktree(self):
        base_text = ''.join('line %d\n' % i for i in range(12))
        base = self.candidate({'code.txt': base_text})
        local = self.candidate({'code.txt': base_text.replace('line 0\n', 'local line\n')}, [base])
        remote = self.candidate({'code.txt': base_text.replace('line 11\n', 'remote line\n')}, [base])
        before = self.files()
        result = self.git('merge-tree', '--write-tree', '--merge-base=' + base, '-z', local, remote, check=False)
        if b'unknown option' in result.stderr or b'unrecognized option' in result.stderr:
            self.skipTest('local-only merge capability unavailable: ' + self.text('--version'))
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        tree = result.stdout.split(b'\0')[0].decode()
        merged = self.text('show', tree + ':code.txt')
        self.assertIn('local line', merged)
        self.assertIn('remote line', merged)
        conflicting = self.candidate({'code.txt': base_text.replace('line 0\n', 'other local line\n')}, [base])
        conflict = self.git('merge-tree', '--write-tree', '--merge-base=' + base, '-z', local, conflicting, check=False)
        self.assertEqual(conflict.returncode, 1)
        self.assertIn(b'code.txt', conflict.stdout)
        self.assertEqual(self.files(), before)

    def test_embedded_repo_is_a_gitlink_and_requires_explicit_support(self):
        nested = self.repo / 'vendor'
        nested.mkdir()
        subprocess.run(['git', 'init', '-q', str(nested)], env=self.env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (nested / 'x.txt').write_text('nested source\n')
        subprocess.run(['git', '-C', str(nested), 'add', '.'], env=self.env, check=True)
        subprocess.run(['git', '-C', str(nested), 'commit', '-qm', 'nested'], env=self.env, check=True)
        self.git('read-tree', 'HEAD', private=True)
        self.git('add', '-A', private=True)
        self.assertIn('160000 ', self.text('ls-files', '--stage', private=True))

    def test_private_default_excludes_keep_git_precedence_and_user_index(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hpclib' / 'servers'))
        from sync_spec import DEFAULT_EXCLUDES
        combined = self.base / 'combined-excludes'
        combined.write_text('\n'.join(DEFAULT_EXCLUDES) + '\nglobal.tmp\n')
        (self.repo / '.git/info/exclude').write_text('repository.tmp\n')
        (self.repo / '.gitignore').write_text('ignored/\n!.DS_Store\n')
        (self.repo / '.DS_Store').write_text('explicitly included by user\n')
        (self.repo / 'global.tmp').write_text('excluded globally\n')
        (self.repo / 'repository.tmp').write_text('excluded by info/exclude\n')
        (self.repo / '__pycache__').mkdir()
        (self.repo / '__pycache__' / 'cache.pyc').write_bytes(b'cache')
        (self.repo / 'nested').mkdir()
        (self.repo / 'nested/.gitignore').write_text('special.txt\n')
        (self.repo / 'nested/special.txt').write_text('excluded by nested rule\n')
        before_index = (self.repo / '.git/index').read_bytes()
        listing = self.text('-c', 'core.excludesFile=' + str(combined), 'ls-files', '--others', '--exclude-standard')
        self.assertIn('.DS_Store', listing)
        self.assertNotIn('global.tmp', listing)
        self.assertNotIn('repository.tmp', listing)
        self.assertNotIn('cache.pyc', listing)
        self.assertNotIn('special.txt', listing)
        self.assertEqual((self.repo / '.git/index').read_bytes(), before_index)


if __name__ == '__main__':
    print(subprocess.check_output(['git', '--version'], env=dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull)).decode().strip(), flush=True)
    unittest.main()
