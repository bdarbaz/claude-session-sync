import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import claude_session_sync as css  # noqa: E402

ACCT_A = 'aaaaaaaa-0000-4000-8000-000000000001'
ORG_A = 'aaaaaaaa-0000-4000-8000-0000000000a0'
ACCT_B = 'bbbbbbbb-0000-4000-8000-000000000002'
ORG_B = 'bbbbbbbb-0000-4000-8000-0000000000b0'


def record(sid, t, account, **extra):
    r = {
        'sessionId': sid, 'cliSessionId': sid + '-cli', 'cwd': '/work', 'originCwd': '/work',
        'createdAt': 1000, 'lastActivityAt': t, 'lastFocusedAt': t,
        'title': 'Disk temizliği ' + sid, 'isArchived': False,
        'remoteMcpServersConfig': [{'name': 'connector-of-' + account}],
        'enabledMcpTools': {'owner': account}, 'toolSurfaceSnapshot': {'owner': account},
        'bridgeSessionIds': ['bridge-' + account], 'error': 'limit hit on ' + account,
    }
    r.update(extra)
    return r


class Env:
    """A fake Claude data dir with two accounts, plus a state dir."""

    def __init__(self, active=ACCT_A):
        self.root = tempfile.mkdtemp(prefix='css-test-')
        self.data = os.path.join(self.root, 'Claude')
        self.state = os.path.join(self.root, 'state')
        for acct, org in ((ACCT_A, ORG_A), (ACCT_B, ORG_B)):
            os.makedirs(self.folder(acct, org))
        self.set_active(active)

    def set_active(self, acct):
        with open(os.path.join(self.data, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump({'lastKnownAccountUuid': acct} if acct else {}, f)

    def folder(self, acct, org):
        return os.path.join(self.data, css.SESSION_DIR, acct, org)

    def put(self, acct, org, rec):
        p = os.path.join(self.folder(acct, org), rec['sessionId'] + '.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(rec, f, ensure_ascii=False)
        return p

    def get(self, acct, org, sid):
        return css.read_json(os.path.join(self.folder(acct, org), sid + '.json'))

    def names(self, acct, org):
        return sorted(n for n in os.listdir(self.folder(acct, org)) if n.startswith('local_'))

    def sync(self, *extra):
        argv = ['--data-dir', self.data, '--state-dir', self.state, '--force'] + list(extra)
        with open(os.devnull, 'w') as devnull:
            old = sys.stdout
            sys.stdout = devnull
            try:
                return css.main(argv)
            finally:
                sys.stdout = old

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def test_sessions_appear_in_both_accounts(self):
        e = self.env
        e.put(ACCT_A, ORG_A, record('local_a1', 100, ACCT_A))
        e.put(ACCT_B, ORG_B, record('local_b1', 200, ACCT_B))
        e.sync()
        self.assertEqual(e.names(ACCT_A, ORG_A), ['local_a1.json', 'local_b1.json'])
        self.assertEqual(e.names(ACCT_B, ORG_B), ['local_a1.json', 'local_b1.json'])

    def test_copies_take_target_account_fields(self):
        e = self.env
        e.put(ACCT_A, ORG_A, record('local_a1', 100, ACCT_A))
        e.put(ACCT_B, ORG_B, record('local_b1', 200, ACCT_B))
        e.sync()
        copy = e.get(ACCT_B, ORG_B, 'local_a1')
        self.assertEqual(copy['remoteMcpServersConfig'], [{'name': 'connector-of-' + ACCT_B}])
        self.assertEqual(copy['enabledMcpTools'], {'owner': ACCT_B})
        self.assertNotIn('bridgeSessionIds', copy)
        self.assertNotIn('error', copy)
        self.assertEqual(copy['cliSessionId'], 'local_a1-cli')
        self.assertEqual(copy['title'], 'Disk temizliği local_a1')

    def test_copy_without_template_drops_account_fields(self):
        e = self.env
        e.put(ACCT_A, ORG_A, record('local_a1', 100, ACCT_A))
        e.sync()
        copy = e.get(ACCT_B, ORG_B, 'local_a1')
        for k in css.ACCOUNT_KEYS:
            self.assertNotIn(k, copy)

    def test_signed_in_account_is_never_edited(self):
        e = self.env  # A signed in
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.sync()
        e.put(ACCT_B, ORG_B, record('local_s', 999, ACCT_B, title='newer in B'))
        before = e.get(ACCT_A, ORG_A, 'local_s')
        e.sync()
        self.assertEqual(e.get(ACCT_A, ORG_A, 'local_s'), before)

    def test_updates_flow_into_signed_out_account(self):
        e = self.env  # A signed in
        e.put(ACCT_B, ORG_B, record('local_b0', 50, ACCT_B))  # gives B a template
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.sync()
        e.put(ACCT_A, ORG_A, record('local_s', 300, ACCT_A, title='renamed', isArchived=True,
                                    cliSessionId='new-cli'))
        e.sync()
        b = e.get(ACCT_B, ORG_B, 'local_s')
        self.assertEqual((b['title'], b['cliSessionId'], b['lastActivityAt']), ('renamed', 'new-cli', 300))
        self.assertTrue(b['isArchived'])
        self.assertEqual(b['enabledMcpTools'], {'owner': ACCT_B})
        idx = css.read_json(os.path.join(e.folder(ACCT_B, ORG_B), css.ARCHIVE_INDEX))
        self.assertEqual(idx, {'v': 1, 'archived': ['local_s']})

    def test_older_copy_never_overwrites_newer(self):
        e = self.env  # A signed in
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A, title='old'))
        e.put(ACCT_B, ORG_B, record('local_s', 500, ACCT_B, title='newer'))
        e.sync()
        self.assertEqual(e.get(ACCT_B, ORG_B, 'local_s')['title'], 'newer')

    def test_unknown_signed_in_account_means_add_only(self):
        e = self.env
        e.set_active(None)
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A, title='A'))
        e.put(ACCT_B, ORG_B, record('local_s', 500, ACCT_B, title='B'))
        e.put(ACCT_B, ORG_B, record('local_b', 500, ACCT_B))
        e.sync()
        self.assertEqual(e.get(ACCT_A, ORG_A, 'local_s')['title'], 'A')
        self.assertIsNotNone(e.get(ACCT_A, ORG_A, 'local_b'))

    def test_delete_moves_other_copies_to_trash(self):
        e = self.env
        p = e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.put(ACCT_A, ORG_A, record('local_keep', 100, ACCT_A))
        e.sync()
        self.assertIn('local_s.json', e.names(ACCT_B, ORG_B))
        os.remove(p)
        e.sync()
        self.assertNotIn('local_s.json', e.names(ACCT_B, ORG_B))
        self.assertNotIn('local_s.json', e.names(ACCT_A, ORG_A))
        self.assertEqual(len(glob.glob(os.path.join(e.state, 'trash', '*', 'local_s.json.*'))), 1)

    def test_deleting_the_last_session_propagates(self):
        e = self.env
        p = e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.sync()
        os.remove(p)
        e.sync()
        self.assertEqual(e.names(ACCT_A, ORG_A), [])
        self.assertEqual(e.names(ACCT_B, ORG_B), [])

    def test_missing_folder_is_not_a_deletion(self):
        e = self.env
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.sync()
        shutil.rmtree(e.folder(ACCT_A, ORG_A))
        e.sync()
        self.assertEqual(e.names(ACCT_B, ORG_B), ['local_s.json'])

    def test_unparseable_file_is_not_a_deletion(self):
        e = self.env
        p = e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.sync()
        with open(p, 'w') as f:
            f.write('{"half written')
        e.sync()
        self.assertEqual(e.names(ACCT_B, ORG_B), ['local_s.json'])
        with open(p) as f:
            self.assertEqual(f.read(), '{"half written')  # left for the app to deal with

    def test_first_run_backs_up(self):
        e = self.env
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.sync()
        self.assertEqual(len(glob.glob(os.path.join(e.state, 'backups', '*', ACCT_A, ORG_A, 'local_s.json'))), 1)

    def test_dry_run_writes_nothing(self):
        e = self.env
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.sync('--dry-run')
        self.assertEqual(e.names(ACCT_B, ORG_B), [])
        self.assertFalse(os.path.exists(os.path.join(e.state, 'state.json')))

    def test_exclude_leaves_folder_alone(self):
        e = self.env
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.sync('--exclude', ACCT_B)
        self.assertEqual(e.names(ACCT_B, ORG_B), [])

    def test_extra_org_folder_of_same_account(self):
        e = self.env
        org2 = 'bbbbbbbb-0000-4000-8000-0000000000b1'
        os.makedirs(e.folder(ACCT_B, org2))
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.put(ACCT_B, ORG_B, record('local_b', 100, ACCT_B))
        e.sync()
        copy = e.get(ACCT_B, org2, 'local_s')
        self.assertEqual(copy['enabledMcpTools'], {'owner': ACCT_B})  # template from B's other org

    def test_ignores_non_session_files(self):
        e = self.env
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        for junk in ('scheduled-tasks.json', 'local_x.json.tmp', 'notes.txt'):
            with open(os.path.join(e.folder(ACCT_A, ORG_A), junk), 'w') as f:
                f.write('{}')
        with open(os.path.join(e.folder(ACCT_A, ORG_A), 'local_broken.json'), 'w') as f:
            f.write('{not json')
        e.sync()
        self.assertEqual(e.names(ACCT_B, ORG_B), ['local_s.json'])
        self.assertFalse(os.path.exists(os.path.join(e.folder(ACCT_B, ORG_B), 'scheduled-tasks.json')))

    def test_write_skips_when_file_changed_underneath(self):
        e = self.env
        p = e.put(ACCT_B, ORG_B, record('local_s', 100, ACCT_B))
        f = css.Folder(e.data, ACCT_B, ORG_B)
        f.load()
        syncer = css.Syncer(e.data, css.State(e.state), e.state, css.Logger(None))
        ok = syncer.write(f, 'local_s.json', {'sessionId': 'local_s', 'title': 'x'},
                          expect_mtime=f.mtimes['local_s.json'] - 1, why='update')
        self.assertFalse(ok)
        self.assertEqual(css.read_json(p)['lastActivityAt'], 100)

    def test_second_process_does_not_get_lock(self):
        first = css.acquire_lock(self.env.state)
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(css.acquire_lock(self.env.state))
        finally:
            first.close()

    def test_old_state_file_is_set_aside(self):
        e = self.env
        os.makedirs(e.state)
        with open(os.path.join(e.state, 'state.json'), 'w') as f:
            json.dump({'seen': {'x': ['local_s.json']}}, f)
        e.put(ACCT_A, ORG_A, record('local_s', 100, ACCT_A))
        e.sync()
        self.assertTrue(os.path.exists(os.path.join(e.state, 'state.json.old')))
        self.assertEqual(css.read_json(os.path.join(e.state, 'state.json'))['version'], css.STATE_VERSION)


class DiscoveryTests(unittest.TestCase):
    def test_macos_path(self):
        dirs = css.candidate_data_dirs('darwin', {}, '/Users/x')
        self.assertEqual(dirs, [os.path.join('/Users/x', 'Library', 'Application Support', 'Claude')])

    def test_linux_path_honours_xdg(self):
        self.assertEqual(css.candidate_data_dirs('linux', {}, '/home/x'),
                         [os.path.join('/home/x', '.config', 'Claude')])
        self.assertEqual(css.candidate_data_dirs('linux', {'XDG_CONFIG_HOME': '/cfg'}, '/home/x'),
                         [os.path.join('/cfg', 'Claude')])

    def test_windows_store_and_classic_paths(self):
        root = tempfile.mkdtemp(prefix='css-win-')
        try:
            local = os.path.join(root, 'Local')
            msix = os.path.join(local, 'Packages', 'Claude_pzs8sxrjxfjjc', 'LocalCache', 'Roaming', 'Claude')
            os.makedirs(msix)
            env = {'LOCALAPPDATA': local, 'APPDATA': os.path.join(root, 'Roaming')}
            self.assertEqual(css.candidate_data_dirs('win32', env, root),
                             [msix, os.path.join(root, 'Roaming', 'Claude')])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_env_override_first(self):
        self.assertEqual(css.candidate_data_dirs('linux', {'CLAUDE_USER_DATA_DIR': '/d'}, '/h')[0], '/d')

    def test_only_uuid_folders_are_used(self):
        e = Env()
        try:
            os.makedirs(os.path.join(e.data, css.SESSION_DIR, 'not-a-uuid', ORG_A))
            os.makedirs(os.path.join(e.data, css.SESSION_DIR, ACCT_A, 'imported-staging'))
            keys = [f.key for f in css.discover_folders(e.data)]
            self.assertEqual(keys, [ACCT_A + '/' + ORG_A, ACCT_B + '/' + ORG_B])
        finally:
            e.cleanup()


if __name__ == '__main__':
    unittest.main()
