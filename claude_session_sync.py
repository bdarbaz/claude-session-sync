#!/usr/bin/env python3
"""claude-session-sync: see and resume Claude desktop Code sessions from every
account you sign in with on the same computer.

The Claude desktop app keeps Code-tab transcripts in ~/.claude/projects, which every
account on the computer shares, but it lists sessions per account and organization from
<userData>/claude-code-sessions/<accountUuid>/<orgUuid>/local_*.json. Switch accounts
and the sidebar is empty even though the transcripts are still on disk.

This tool mirrors those small sidebar records between all account/org folders:

- folders of the signed-in account only ever receive new files, never edits, because
  the running app holds those records in memory and would overwrite an edit;
- other folders receive new files and updates, taken from a copy at least as fresh;
- fields tied to one account (connectors, Remote Control, quota errors) never cross over;
- a session deleted in one folder is moved to this tool's trash in the others;
- the first run backs up every folder it touches.

Unofficial; not affiliated with Anthropic. Standard library only, Python 3.8+.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time

VERSION = '1.0.0'

SESSION_DIR = 'claude-code-sessions'
ARCHIVE_INDEX = 'archived-sessions.idx'
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
SESSION_FILE_RE = re.compile(r'^local_[A-Za-z0-9_-]+\.json$')
# Windows Store (MSIX) builds redirect %APPDATA% into their package folder.
MSIX_PACKAGE_GLOB = '*Claude_*'

# Tied to the account that owns the sidebar entry; never copied across.
ACCOUNT_KEYS = frozenset({
    'remoteMcpServersConfig', 'enabledMcpTools', 'toolSurfaceSnapshot',
    'bridgeSessionIds', 'remoteControlAutoEligible', 'steeredByRemoteClient',
    'error', 'errorAt', 'priorErrorMark',
})
# Of those, what a new copy takes from the target folder's own sessions.
TEMPLATE_KEYS = ('remoteMcpServersConfig', 'enabledMcpTools', 'toolSurfaceSnapshot')

STATE_VERSION = 2
STALE_TMP_SECONDS = 600


# ---------------------------------------------------------------- small helpers

def now_ms():
    return int(time.time() * 1000)


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def dump_json(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def atomic_write(path, text):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    # Not named local_*.json.tmp: the app treats those as its own crash-recovery files.
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.sync-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def freshness(record):
    vals = [record.get('lastActivityAt'), record.get('lastFocusedAt')]
    return max([v for v in vals if isinstance(v, (int, float))] or [0])


def short(uuid):
    return uuid[:8]


class Logger:
    def __init__(self, path, echo=False):
        self.path = path
        self.echo = echo

    def __call__(self, msg):
        line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg
        if self.echo:
            print(line)
        if not self.path:
            return
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > 1_000_000:
                os.replace(self.path, self.path + '.1')
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass


# ---------------------------------------------------------------- discovery

def candidate_data_dirs(platform=None, env=None, home=None):
    """Where the Claude desktop app keeps its data on this OS, most specific first."""
    platform = platform or ('win32' if os.name == 'nt' else sys.platform)
    env = os.environ if env is None else env
    home = home or os.path.expanduser('~')
    out = []
    if env.get('CLAUDE_USER_DATA_DIR'):
        out.append(env['CLAUDE_USER_DATA_DIR'])
    if platform == 'darwin':
        out.append(os.path.join(home, 'Library', 'Application Support', 'Claude'))
    elif platform == 'win32':
        if env.get('LOCALAPPDATA'):
            out += sorted(glob.glob(os.path.join(
                env['LOCALAPPDATA'], 'Packages', MSIX_PACKAGE_GLOB, 'LocalCache', 'Roaming', 'Claude')))
        if env.get('APPDATA'):
            out.append(os.path.join(env['APPDATA'], 'Claude'))
    else:
        xdg = env.get('XDG_CONFIG_HOME') or os.path.join(home, '.config')
        out.append(os.path.join(xdg, 'Claude'))
    return out


def find_data_dirs(explicit=None):
    dirs = []
    for d in (explicit or candidate_data_dirs()):
        d = os.path.abspath(os.path.expanduser(d))
        if d not in dirs and os.path.isdir(os.path.join(d, SESSION_DIR)):
            dirs.append(d)
    return dirs


def active_account(data_dir):
    cfg = read_json(os.path.join(data_dir, 'config.json'), {})
    acct = cfg.get('lastKnownAccountUuid') if isinstance(cfg, dict) else None
    return acct if isinstance(acct, str) and UUID_RE.match(acct) else None


class Folder:
    """One <account>/<org> sidebar folder."""

    def __init__(self, data_dir, account, org):
        self.account = account
        self.org = org
        self.key = account + '/' + org
        self.path = os.path.join(data_dir, SESSION_DIR, account, org)
        self.sessions = {}  # file name -> record
        self.mtimes = {}    # file name -> st_mtime_ns when read
        self.present = set()  # every local_*.json on disk, parseable or not
        self.readable = False

    def label(self):
        return '%s/%s' % (short(self.account), short(self.org))

    def load(self):
        self.sessions.clear()
        self.mtimes.clear()
        self.present.clear()
        try:
            names = os.listdir(self.path)
        except OSError:
            self.readable = False
            return
        self.readable = True
        for name in names:
            p = os.path.join(self.path, name)
            if name.startswith('.sync-') and name.endswith('.tmp'):
                try:
                    if time.time() - os.path.getmtime(p) > STALE_TMP_SECONDS:
                        os.unlink(p)
                except OSError:
                    pass
                continue
            if not SESSION_FILE_RE.match(name):
                continue
            self.present.add(name)
            try:
                mtime = os.stat(p).st_mtime_ns
            except OSError:
                continue
            rec = read_json(p)
            if isinstance(rec, dict) and isinstance(rec.get('sessionId'), str):
                self.sessions[name] = rec
                self.mtimes[name] = mtime
            # Anything else is mid-write or not ours to judge; the next run looks again.


def discover_folders(data_dir, exclude=()):
    root = os.path.join(data_dir, SESSION_DIR)
    folders = []
    try:
        accounts = sorted(os.listdir(root))
    except OSError:
        return folders
    for acct in accounts:
        if not UUID_RE.match(acct) or acct in exclude:
            continue
        try:
            orgs = sorted(os.listdir(os.path.join(root, acct)))
        except OSError:
            continue
        for org in orgs:
            if UUID_RE.match(org) and org not in exclude and \
                    os.path.isdir(os.path.join(root, acct, org)):
                folders.append(Folder(data_dir, acct, org))
    return folders


# ---------------------------------------------------------------- state

class State:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, 'state.json')
        data = read_json(self.path, {})
        host = socket.gethostname()
        if not isinstance(data, dict) or data.get('version') != STATE_VERSION or \
                data.get('host') not in (None, host):
            if data and os.path.exists(self.path):
                try:
                    os.replace(self.path, self.path + '.old')
                except OSError:
                    pass
            data = {'version': STATE_VERSION, 'dirs': {}}
        data['host'] = host
        self.data = data

    def dir(self, data_dir):
        d = self.data['dirs'].setdefault(data_dir, {})
        for k in ('seen', 'copied', 'templates'):
            d.setdefault(k, {})
        return d

    def save(self):
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))


def fingerprint(data_dir, folders, active):
    parts = [active or '-']
    for f in folders:
        try:
            names = sorted(os.listdir(f.path))
        except OSError:
            names = []
        parts.append(f.key)
        for n in names:
            if n.startswith('.'):
                continue
            try:
                st = os.stat(os.path.join(f.path, n))
                parts.append('%s:%d:%d' % (n, st.st_mtime_ns, st.st_size))
            except OSError:
                pass
    return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------- sync

def fresh_copy(src, template):
    c = {k: v for k, v in src.items() if k not in ACCOUNT_KEYS}
    for k in TEMPLATE_KEYS:
        if template and k in template:
            c[k] = template[k]
    return c


def merged(src, dst):
    m = {k: v for k, v in src.items() if k not in ACCOUNT_KEYS}
    m.update({k: v for k, v in dst.items() if k in ACCOUNT_KEYS})
    return m


class Syncer:
    def __init__(self, data_dir, state, state_dir, log, dry_run=False, exclude=()):
        self.data_dir = data_dir
        self.st = state.dir(data_dir)
        self.state_dir = state_dir
        self.log = log
        self.dry_run = dry_run
        self.exclude = set(exclude)
        self.changes = 0

    # -- writes (all funnel through here so --dry-run is honest)

    def write(self, folder, name, record, expect_mtime=None, why=''):
        path = os.path.join(folder.path, name)
        self.changes += 1
        self.log('%s %s "%s" in %s' % (why, name, record.get('title', ''), folder.label()))
        if self.dry_run:
            return True
        try:
            if expect_mtime is None:
                if os.path.exists(path):
                    return False
            elif os.stat(path).st_mtime_ns != expect_mtime:
                self.log('  skipped: %s changed while syncing; retrying next run' % name)
                return False
            atomic_write(path, dump_json(record))
            folder.mtimes[name] = os.stat(path).st_mtime_ns
            return True
        except OSError as e:
            self.log('  failed: %s (%s); retrying next run' % (name, e))
            return False

    def trash(self, folder, name, reason):
        self.changes += 1
        self.log('trash %s from %s (%s)' % (name, folder.label(), reason))
        if self.dry_run:
            return
        dst_dir = os.path.join(self.state_dir, 'trash', folder.label().replace('/', '-'))
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, '%s.%d' % (name, now_ms()))
        try:
            shutil.move(os.path.join(folder.path, name), dst)
        except OSError as e:
            self.log('  failed: %s' % e)

    def write_index(self, folder, archived_ids, replace):
        path = os.path.join(folder.path, ARCHIVE_INDEX)
        idx = read_json(path, None)
        current = idx.get('archived') if isinstance(idx, dict) and \
            isinstance(idx.get('archived'), list) else []
        want = sorted(set(archived_ids)) if replace else sorted(set(current) | set(archived_ids))
        if sorted(current) == want or (not want and not os.path.exists(path)):
            return
        if self.dry_run:
            return
        try:
            atomic_write(path, dump_json({'v': 1, 'archived': want}))
        except OSError as e:
            self.log('  failed to update %s in %s: %s' % (ARCHIVE_INDEX, folder.label(), e))

    def backup(self, folders):
        if self.st.get('backedUp') or self.dry_run:
            return
        tag = hashlib.sha1(self.data_dir.encode('utf-8')).hexdigest()[:8]
        dst = os.path.join(self.state_dir, 'backups', time.strftime('%Y%m%d-%H%M%S') + '-' + tag)
        for f in folders:
            if os.path.isdir(f.path):
                shutil.copytree(f.path, os.path.join(dst, f.account, f.org),
                                ignore=shutil.ignore_patterns('.sync-*'), dirs_exist_ok=True)
        self.st['backedUp'] = True
        self.log('backed up %s -> %s' % (self.data_dir, dst))

    # -- the run

    def templates(self, folders):
        """Each folder's own connector setup, from a session the app created there."""
        for f in folders:
            copied = set(self.st['copied'].get(f.key, []))
            native = [r for n, r in f.sessions.items()
                      if n not in copied and r.get('remoteMcpServersConfig') is not None]
            if native:
                best = max(native, key=freshness)
                self.st['templates'][f.key] = {k: best[k] for k in TEMPLATE_KEYS if k in best}
        by_account = {}
        for f in folders:
            if f.key in self.st['templates']:
                by_account.setdefault(f.account, self.st['templates'][f.key])
        return {f.key: self.st['templates'].get(f.key) or by_account.get(f.account)
                for f in folders}

    def run(self, force=False):
        folders = discover_folders(self.data_dir, self.exclude)
        if len(folders) < 2:
            return 0
        active = active_account(self.data_dir)
        fp = fingerprint(self.data_dir, folders, active)
        if not force and self.st.get('fingerprint') == fp:
            return 0
        for f in folders:
            f.load()
        self.backup(folders)
        templates = self.templates(folders)

        # 1. A session deleted in one folder goes to trash everywhere else.
        for f in folders:
            gone = set(self.st['seen'].get(f.key, [])) - f.present
            if not gone or not f.readable:  # a missing folder is not "everything deleted"
                continue
            for name in sorted(gone):
                for g in folders:
                    if g is not f and name in g.sessions:
                        self.trash(g, name, 'deleted in %s' % f.label())
                        del g.sessions[name]
                        g.present.discard(name)

        # 2. Every folder gets every session; only inactive folders get updates.
        def rank(item):
            f, rec = item
            return (freshness(rec), f.account == active, f.key)

        added_archived = {}
        names = sorted(set().union(*[f.sessions.keys() for f in folders]))
        for name in names:
            holders = [(f, f.sessions[name]) for f in folders if name in f.sessions]
            best_folder, best = max(holders, key=rank)
            for f in folders:
                cur = f.sessions.get(name)
                if cur is None:
                    rec = fresh_copy(best, templates.get(f.key))
                    if self.write(f, name, rec, why='add'):
                        f.sessions[name] = rec
                        copied = self.st['copied'].setdefault(f.key, [])
                        if name not in copied:
                            copied.append(name)
                        if rec.get('isArchived'):
                            added_archived.setdefault(f.key, []).append(rec['sessionId'])
                    continue
                writable = active is not None and f.account != active
                if not writable or f is best_folder or freshness(best) < freshness(cur):
                    continue
                rec = merged(best, cur)
                if rec != cur and self.write(f, name, rec, expect_mtime=f.mtimes.get(name),
                                             why='update'):
                    f.sessions[name] = rec

        # 3. The archive index is only a load-order hint; each record's isArchived decides.
        for f in folders:
            writable = active is not None and f.account != active
            if writable:
                self.write_index(f, [r['sessionId'] for r in f.sessions.values()
                                     if r.get('isArchived')], replace=True)
            elif f.key in added_archived:
                self.write_index(f, added_archived[f.key], replace=False)

        if not self.dry_run:
            self.st['seen'] = {f.key: sorted(f.sessions) for f in folders}
            self.st['fingerprint'] = fingerprint(self.data_dir, folders, active)
        return self.changes


# ---------------------------------------------------------------- CLI

def acquire_lock(state_dir):
    """One syncing process at a time. Returns a handle to keep open, or None."""
    os.makedirs(state_dir, exist_ok=True)
    f = open(os.path.join(state_dir, 'lock'), 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def load_config(state_dir):
    cfg = read_json(os.path.join(state_dir, 'config.json'), {})
    return cfg if isinstance(cfg, dict) else {}


def sync_all(args, state_dir, log, force=False):
    cfg = load_config(state_dir)
    exclude = set(cfg.get('exclude') or []) | set(args.exclude or [])
    state = State(state_dir)
    total = 0
    for d in find_data_dirs(args.data_dir):
        total += Syncer(d, state, state_dir, log, dry_run=args.dry_run, exclude=exclude).run(force)
    if not args.dry_run:
        state.save()
    return total


def print_status(args, state_dir):
    dirs = find_data_dirs(args.data_dir)
    if not dirs:
        print('No Claude desktop data folder with Code sessions found. Looked in:')
        for d in (args.data_dir or candidate_data_dirs()):
            print('  ' + d)
        return
    exclude = set(load_config(state_dir).get('exclude') or []) | set(args.exclude or [])
    for d in dirs:
        active = active_account(d)
        print(d)
        print('  signed-in account: %s' % (active or 'unknown'))
        for f in discover_folders(d):
            f.load()
            flags = []
            if f.account == active:
                flags.append('signed in')
            if f.account in exclude or f.org in exclude:
                flags.append('excluded')
            print('  account %s  org %s  %3d sessions  %s' % (
                f.account, f.org, len(f.sessions), ', '.join(flags)))
    print('state and logs: ' + state_dir)


def main(argv=None):
    p = argparse.ArgumentParser(
        prog='claude-session-sync',
        description='Mirror Claude desktop Code sessions between the accounts on this computer.')
    p.add_argument('--loop', action='store_true', help='keep running and sync every --interval seconds')
    p.add_argument('--interval', type=float, default=5.0, help='seconds between syncs in --loop (default 5)')
    p.add_argument('--status', action='store_true', help='show detected folders and exit')
    p.add_argument('--dry-run', action='store_true', help='print what would change, write nothing')
    p.add_argument('--force', action='store_true', help='sync even if nothing looks changed')
    p.add_argument('--data-dir', action='append', help='Claude app data folder (repeatable; default: auto-detect)')
    p.add_argument('--exclude', action='append', help='account or org UUID to leave alone (repeatable)')
    p.add_argument('--state-dir', default=os.path.join(os.path.expanduser('~'), '.claude-session-sync'),
                   help='where state, logs, backups and trash go (default ~/.claude-session-sync)')
    p.add_argument('--version', action='version', version='%(prog)s ' + VERSION)
    args = p.parse_args(argv)
    state_dir = os.path.abspath(os.path.expanduser(args.state_dir))
    # Session titles can hold any character; a Windows console (cp1252 etc.) can't print them all.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(errors='replace')
            except (ValueError, OSError):
                pass

    if args.status:
        print_status(args, state_dir)
        return 0
    if args.dry_run:
        n = sync_all(args, state_dir, Logger(None, echo=True), force=True)
        print('%d change(s) would be made.' % n)
        return 0

    lock = acquire_lock(state_dir)
    if lock is None:
        if not args.loop:
            print('Another claude-session-sync is running; it will do the sync.')
        return 0
    try:
        log = Logger(os.path.join(state_dir, 'sync.log'), echo=not args.loop)
        if not args.loop:
            n = sync_all(args, state_dir, log, force=args.force)
            print('%d change(s).' % n)
            return 0
        log('started v%s (pid %d)' % (VERSION, os.getpid()))
        first = True
        while True:
            try:
                sync_all(args, state_dir, log, force=first)
                first = False
            except Exception as e:  # keep the service alive; the next pass retries
                log('error: %r' % (e,))
            time.sleep(max(1.0, args.interval))
    finally:
        lock.close()


if __name__ == '__main__':
    sys.exit(main())
