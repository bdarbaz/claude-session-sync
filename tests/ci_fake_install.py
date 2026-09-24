"""CI helper: fake a Claude desktop data folder with two accounts, then check the
installed background service mirrored them.

    python tests/ci_fake_install.py make
    python tests/ci_fake_install.py check
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import claude_session_sync as css  # noqa: E402

A = ('aaaaaaaa-0000-4000-8000-000000000001', 'aaaaaaaa-0000-4000-8000-0000000000a0')
B = ('bbbbbbbb-0000-4000-8000-000000000002', 'bbbbbbbb-0000-4000-8000-0000000000b0')


def data_dir():
    return css.candidate_data_dirs()[-1]  # the classic, non-Store location on Windows


def folder(pair):
    return os.path.join(data_dir(), css.SESSION_DIR, pair[0], pair[1])


def make():
    for pair, sid in ((A, 'local_ci-a'), (B, 'local_ci-b')):
        os.makedirs(folder(pair), exist_ok=True)
        rec = {'sessionId': sid, 'cliSessionId': sid, 'cwd': '/tmp', 'originCwd': '/tmp',
               'createdAt': 1, 'lastActivityAt': 2, 'title': 'CI ' + sid}
        with open(os.path.join(folder(pair), sid + '.json'), 'w', encoding='utf-8') as f:
            json.dump(rec, f)
    with open(os.path.join(data_dir(), 'config.json'), 'w', encoding='utf-8') as f:
        json.dump({'lastKnownAccountUuid': A[0]}, f)
    print('fake data in ' + data_dir())


def check():
    want = ['local_ci-a.json', 'local_ci-b.json']
    deadline = time.time() + 30
    while time.time() < deadline:
        got = [sorted(n for n in os.listdir(folder(p)) if n.startswith('local_')) for p in (A, B)]
        if got == [want, want]:
            print('ok: both accounts list both sessions')
            return 0
        time.sleep(1)
    print('FAIL: ' + repr(got))
    return 1


if __name__ == '__main__':
    sys.exit(make() if sys.argv[1:] == ['make'] else check())
