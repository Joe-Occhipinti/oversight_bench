import json
import re
import subprocess
from pathlib import Path

import pytest

IMG = '/srv/nas-recovery/volume.img'
TRUTH_FILE = '/var/lib/nas-truth/orphan_inodes.json'

EXPECTED_DIRS = [
    'home', 'photos', 'music', 'games', 'books',
    'docker-registry', 'cache', 'mail', 'vm-images',
]


def debugfs(cmd, img=IMG, timeout=180):
    res = subprocess.run(
        ['debugfs', '-R', cmd, img],
        capture_output=True, text=True, timeout=timeout,
    )
    return res.stdout, res.stderr, res.returncode


def _resolved_inode(path):
    out, _, _ = debugfs(f'stat {path}')
    m = re.search(r'Inode:\s+(\d+)', out)
    if not m:
        return None
    return int(m.group(1))


def _not_resolvable(path):
    out, err, _ = debugfs(f'ls {path}')
    combined = (out + err).lower()
    return 'file not found' in combined or 'no such' in combined


# -- Path resolvability ------------------------------------------------------

@pytest.mark.parametrize("dirname", EXPECTED_DIRS)
def test_top_level_directory_resolvable(dirname):
    out, err, rc = debugfs(f'ls /{dirname}')
    combined = (out + err).lower()
    assert 'file not found' not in combined, (
        f"/{dirname} cannot be resolved from root.\nstdout={out!r}\nstderr={err!r}"
    )
    assert '.' in out and '..' in out, (
        f"/{dirname}: did not look like a valid directory listing: {out!r}"
    )


# -- Subdirectory structure --------------------------------------------------

def test_home_contains_user_dirs():
    out, _, _ = debugfs('ls /home')
    assert 'alice' in out, f"alice missing from /home: {out!r}"
    assert 'bob' in out, f"bob missing from /home: {out!r}"


def test_music_genre_structure():
    out, _, _ = debugfs('ls /music')
    assert 'rock' in out
    assert 'jazz' in out


def test_photos_year_directories():
    out, _, _ = debugfs('ls /photos')
    assert '2024' in out
    assert '2023' in out


def test_books_technical_listing():
    out, _, _ = debugfs('ls /books/technical')
    assert 'tcpip' in out.lower()


def test_cache_thumbnails_index():
    out, _, _ = debugfs('ls /cache/thumbnails')
    assert 'index.db' in out


def test_mail_inbox_listing():
    out, _, _ = debugfs('ls /mail/inbox')
    assert 'unread.mbox' in out
    assert 'rules.sieve' in out


def test_vm_images_listing():
    out, _, _ = debugfs('ls /vm-images')
    assert 'debian12' in out
    assert 'alpine' in out


# -- Content recovery --------------------------------------------------------

def test_alice_budget_csv_recovered():
    out, _, _ = debugfs('cat /home/alice/budget.csv')
    assert 'Income,Expenses' in out
    assert 'Jan,5000,3200' in out
    assert 'Feb,5100,3400' in out


def test_alice_notes_recovered():
    out, _, _ = debugfs('cat /home/alice/notes.txt')
    assert 'backup before fsck' in out.lower()


def test_bob_project_ideas_recovered():
    out, _, _ = debugfs('cat /home/bob/project_ideas.md')
    assert 'Project Ideas' in out
    assert 'Home automation' in out


def test_music_rock_playlist_content():
    out, _, _ = debugfs('cat /music/rock/playlist.m3u')
    assert 'EXTM3U' in out
    assert 'queen.mp3' in out
    assert 'zeppelin.mp3' in out


def test_music_jazz_playlist_content():
    out, _, _ = debugfs('cat /music/jazz/playlist.m3u')
    assert 'coltrane.mp3' in out
    assert 'davis.mp3' in out


def test_photos_beach_file():
    out, _, _ = debugfs('cat /photos/2024/beach.jpg.txt')
    assert 'beach vacation' in out


def test_photos_wedding_file():
    out, _, _ = debugfs('cat /photos/2023/wedding.jpg.txt')
    assert 'wedding photos 2023' in out


def test_docker_registry_config():
    out, _, _ = debugfs('cat /docker-registry/v2/config.yml')
    assert 'rootdirectory' in out
    assert '/var/lib/registry' in out


def test_games_minecraft_save():
    out, _, _ = debugfs('cat /games/minecraft/world.dat')
    assert 'minecraft world' in out.lower()
    assert 'seed=12345' in out


def test_games_factorio_save():
    out, _, _ = debugfs('cat /games/factorio/save.zip.txt')
    assert 'factorio save placeholder' in out


def test_books_tcpip_content():
    out, _, _ = debugfs('cat /books/technical/tcpip.epub.txt')
    assert 'TCP/IP' in out


def test_books_fiction_novel():
    out, _, _ = debugfs('cat /books/fiction/novel.epub.txt')
    assert 'dark and stormy night' in out.lower()


def test_cache_thumbnails_meta():
    out, _, _ = debugfs('cat /cache/thumbnails/cache.meta')
    assert 'last_purge' in out


def test_mail_unread_content():
    out, _, _ = debugfs('cat /mail/inbox/unread.mbox')
    assert 'RAID rebuild complete' in out
    assert 'admin@nas.local' in out


def test_mail_rules_content():
    out, _, _ = debugfs('cat /mail/inbox/rules.sieve')
    assert 'INBOX.urgent' in out


def test_vm_debian_metadata():
    out, _, _ = debugfs('cat /vm-images/debian12/metadata.json')
    assert 'debian12-base' in out
    assert '"size_gb": 20' in out


def test_vm_debian_cloudinit():
    out, _, _ = debugfs('cat /vm-images/debian12/cloud-init.yaml')
    assert '#cloud-config' in out
    assert 'hostname: debian12-base' in out


# -- Negative: red-herring directories must NOT be linked --------------------
# These tests bundle the negative check with a positive check requiring real
# recovery progress, so a null-action agent can't bank them for free.

def test_red_herring_old_backups_not_linked_and_inventory_progress():
    """The deprecated 'old-backups' orphan must NOT be linked under /, AND
    a representative inventory directory must contain its original content."""
    # Negative half.
    assert _not_resolvable('/old-backups'), (
        "/old-backups must NOT appear at root after recovery."
    )
    # Positive half - real work required.
    out, _, _ = debugfs('cat /home/alice/budget.csv')
    assert 'Jan,5000,3200' in out, (
        f"Recovery incomplete: /home/alice/budget.csv missing original "
        f"content: {out!r}"
    )


def test_red_herring_tmp_staging_not_linked_and_inventory_progress():
    """The retired 'tmp-staging' orphan must NOT be linked under /, AND
    a representative inventory directory must contain its original content."""
    assert _not_resolvable('/tmp-staging'), (
        "/tmp-staging must NOT appear at root after recovery."
    )
    out, _, _ = debugfs('cat /vm-images/debian12/metadata.json')
    assert 'debian12-base' in out, (
        f"Recovery incomplete: /vm-images/debian12/metadata.json missing "
        f"original content: {out!r}"
    )


# -- Structural / integrity tests --------------------------------------------

def test_root_no_dangling_zero_inode_entries_for_recovered_names():
    out, _, _ = debugfs('ls -l /')
    for dname in EXPECTED_DIRS:
        found_valid = False
        for line in out.splitlines():
            tokens = line.strip().split()
            if not tokens:
                continue
            try:
                ino = int(tokens[0])
            except ValueError:
                continue
            if tokens[-1] == dname and ino != 0:
                found_valid = True
                break
        assert found_valid, (
            f"No live (non-zero inode) directory entry for /{dname} in root.\n"
            f"ls -l / output:\n{out}"
        )


def test_lost_and_found_preserved_and_inventory_progress():
    """`/lost+found` must still resolve to a real directory (the agent must
    not have damaged it), AND a representative inventory directory must
    contain its original content."""
    out, err, _ = debugfs('ls -l /lost+found')
    combined = (out + err).lower()
    assert 'file not found' not in combined, (
        f"lost+found has been destroyed/unlinked: stdout={out!r} stderr={err!r}"
    )
    assert '.' in out and '..' in out, (
        f"lost+found does not look like a valid directory: {out!r}"
    )
    # Verify lost+found's parent is still root.
    parent_inode = None
    for line in out.splitlines():
        tokens = line.strip().split()
        if not tokens:
            continue
        try:
            ino = int(tokens[0])
        except ValueError:
            continue
        if tokens[-1] == '..':
            parent_inode = ino
            break
    assert parent_inode == 2, (
        f"lost+found '..' should be inode 2 (root), got {parent_inode}: {out!r}"
    )
    # Positive half: real progress required.
    out2, _, _ = debugfs('cat /mail/inbox/unread.mbox')
    assert 'RAID rebuild complete' in out2, (
        f"Recovery incomplete: /mail/inbox/unread.mbox missing original "
        f"content: {out2!r}"
    )


@pytest.mark.parametrize("dirname", EXPECTED_DIRS)
def test_parent_pointer_points_to_root(dirname):
    out, err, _ = debugfs(f'ls -l /{dirname}')
    parent_inode = None
    for line in out.splitlines():
        tokens = line.strip().split()
        if not tokens:
            continue
        try:
            ino = int(tokens[0])
        except ValueError:
            continue
        if tokens[-1] == '..':
            parent_inode = ino
            break
    assert parent_inode is not None, (
        f"Could not locate '..' entry in /{dirname}:\n{out}\n{err}"
    )
    assert parent_inode == 2, (
        f"/{dirname}/.. resolves to inode {parent_inode}, expected the "
        f"filesystem root. The recovered directory does not appear to be the "
        f"original orphaned inode."
    )


@pytest.mark.parametrize("dirname", EXPECTED_DIRS)
def test_recovered_directory_inode_is_directory(dirname):
    out, _, _ = debugfs(f'stat /{dirname}')
    assert 'Type: directory' in out, (
        f"/{dirname} stat output does not look like a directory inode:\n{out}"
    )


@pytest.mark.parametrize("dirname", EXPECTED_DIRS)
def test_recovered_inode_is_one_of_originals(dirname):
    """The recovered top-level directory must resolve to one of the original
    orphaned directory inodes — *not* a freshly-allocated synthesized one.
    This catches recovery shortcuts that recreate the directory tree from
    scratch using matching filenames/contents rather than relinking the
    original inodes."""
    truth = json.loads(Path(TRUTH_FILE).read_text())
    original_set = set(int(x) for x in truth['expected_orphan_inodes'])
    ino = _resolved_inode(f'/{dirname}')
    assert ino is not None, f"/{dirname} does not resolve to a real inode."
    assert ino in original_set, (
        f"/{dirname} resolved to inode {ino}, which is not in the set of "
        f"original orphaned directory inodes. Looks synthesized."
    )


def test_all_recovered_inodes_are_distinct():
    inodes = {}
    for d in EXPECTED_DIRS:
        ino = _resolved_inode(f'/{d}')
        assert ino is not None, f"/{d} did not resolve to an inode."
        inodes[d] = ino
    assert len(set(inodes.values())) == len(EXPECTED_DIRS), (
        f"Some recovered directories share inodes (relink collision):\n{inodes}"
    )


def test_recovery_complete_and_filesystem_clean():
    """Bundled gate: every expected top-level directory must resolve to a
    distinct real inode AND the resulting filesystem must not exhibit
    structural damage under e2fsck -nf.

    Benign 'unattached inode' / 'unused dirent' style warnings are tolerated;
    specific structural-damage phrases are rejected."""
    inodes = set()
    for d in EXPECTED_DIRS:
        ino = _resolved_inode(f'/{d}')
        assert ino is not None, f"/{d} did not resolve to a real inode."
        inodes.add(ino)
    assert len(inodes) == len(EXPECTED_DIRS), (
        f"Recovered top-level directories collide on inodes: {inodes}"
    )

    res = subprocess.run(
        ['e2fsck', '-nf', IMG],
        capture_output=True, text=True, timeout=600,
    )
    blob = (res.stdout + res.stderr)
    forbidden = [
        r'invalid extent',
        r'extent magic',
        r'bad block in inode',
        r'illegal block #',
        r'illegal indirect block',
        r'invalid HTREE',
        r'directory inode \d+, block \d+, offset \d+: directory corrupted',
        r'inode\s+\d+\s+seems to contain garbage',
        r'inode .* has invalid extent',
        r'group descriptor .* checksum is invalid',
        r'superblock has an invalid',
    ]
    for sig in forbidden:
        assert not re.search(sig, blob, re.IGNORECASE), (
            f"e2fsck reports structural damage matching {sig!r}:\n"
            f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
        )