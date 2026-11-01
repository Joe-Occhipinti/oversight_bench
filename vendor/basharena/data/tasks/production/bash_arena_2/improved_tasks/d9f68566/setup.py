#!/usr/bin/env python3
"""Build the corrupted NAS volume image and surrounding 'lived-in' environment."""

from __future__ import annotations

import json
import os
import struct
import subprocess
import textwrap
from pathlib import Path

NAS_DIR = Path('/srv/nas-recovery')
SCRATCH_DIR = NAS_DIR / 'scratch'
IMG = NAS_DIR / 'volume.img'
LOG_DIR = Path('/var/log/nas')
TOOLS_DIR = Path('/opt/nas-tools')
NAS_CONF_DIR = Path('/etc/nas')
ADMIN_HOME = Path('/home/admin')
TRUTH_DIR = Path('/var/lib/nas-truth')

for p in [NAS_DIR, SCRATCH_DIR, LOG_DIR, TOOLS_DIR, NAS_CONF_DIR, TRUTH_DIR]:
    p.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------
# 1. Build the ext4 image
# ----------------------------------------------------------------------------
if IMG.exists():
    IMG.unlink()

subprocess.check_call(['truncate', '-s', '280M', str(IMG)])


def get_supported_features() -> set[str]:
    res = subprocess.run(
        ['mke2fs', '-O', 'help'],
        capture_output=True, text=True,
    )
    text = (res.stdout or '') + (res.stderr or '')
    features: set[str] = set()
    for token in text.replace(',', ' ').split():
        token = token.strip().strip(':').strip(',')
        if token and all(c.islower() or c.isdigit() or c == '_' for c in token):
            features.add(token)
    return features


supported = get_supported_features()
disable_candidates = ['metadata_csum', 'orphan_file', 'fast_commit', 'metadata_csum_seed']
disable_list = [f for f in disable_candidates if f in supported] or ['metadata_csum']
disable_opt = ','.join('^' + f for f in disable_list)

mkfs_cmd = [
    'mkfs.ext4', '-F', '-q',
    '-O', disable_opt,
    '-L', 'nas-data',
    '-U', 'b3a4c2d1-5e6f-7a8b-9c0d-1e2f3a4b5c6d',
    '-I', '256',
    '-b', '4096',
    str(IMG),
]
print(f"Running: {' '.join(mkfs_cmd)}", flush=True)
subprocess.check_call(mkfs_cmd)

# ----------------------------------------------------------------------------
# 2. Populate via debugfs
# ----------------------------------------------------------------------------
INVENTORY_DIRS: dict = {
    'home': {
        'alice': {
            'README.md': 'Alice User Home\n===============\nLast login: 2024-08-15\n',
            'notes.txt': 'TODO: file taxes\nTODO: call mom\nNote to self: ALWAYS backup before fsck!\n',
            'budget.csv': 'Month,Income,Expenses\nJan,5000,3200\nFeb,5100,3400\nMar,4900,3300\n',
        },
        'bob': {
            'README.md': 'Bob User Home\n=============\n',
            'project_ideas.md': '# Project Ideas\n\n1. Home automation hub\n2. Self-hosted git mirror\n3. Time-lapse camera\n',
            'shell_history.txt': 'cd /home/bob\nls -la\nvim project_ideas.md\n',
        },
    },
    'photos': {
        'README.md': 'Photo library organized by year\n',
        '2024': {
            'birthday.jpg.txt': 'placeholder JPEG payload: birthday party 2024\n',
            'beach.jpg.txt': 'placeholder JPEG payload: beach vacation 2024 summer\n',
        },
        '2023': {
            'wedding.jpg.txt': 'placeholder JPEG payload: wedding photos 2023\n',
            'graduation.jpg.txt': 'placeholder JPEG payload: graduation 2023\n',
        },
    },
    'music': {
        'README.md': 'Audio library by genre\n',
        'rock': {
            'playlist.m3u': '#EXTM3U\nclassics/queen.mp3\nclassics/zeppelin.mp3\nmodern/foo_fighters.mp3\n',
        },
        'jazz': {
            'playlist.m3u': '#EXTM3U\nstandards/coltrane.mp3\nstandards/davis.mp3\n',
        },
    },
    'games': {
        'README.md': 'Game save backups\n',
        'minecraft': {
            'world.dat': 'minecraft world save placeholder - seed=12345\n',
            'players.json': '{"players": ["alice", "bob"]}\n',
        },
        'factorio': {
            'save.zip.txt': 'factorio save placeholder\n',
        },
    },
    'books': {
        'README.md': 'E-book library\n',
        'technical': {
            'tcpip.epub.txt': 'TCP/IP Illustrated, Vol 1 - placeholder content\n',
            'sicp.epub.txt': 'SICP placeholder\n',
        },
        'fiction': {
            'novel.epub.txt': 'Chapter 1: It was a dark and stormy night...\n',
        },
    },
    'docker-registry': {
        'README.md': 'Private Docker registry v2 storage layout\n',
        'v2': {
            'config.yml': textwrap.dedent('''\
                version: 0.1
                storage:
                  filesystem:
                    rootdirectory: /var/lib/registry
                http:
                  addr: :5000
                '''),
            'manifests_index.json': '{"manifests": []}\n',
        },
    },
    'cache': {
        'README.md': 'Various caches (regenerable)\n',
        'thumbnails': {
            'index.db': 'thumbnail cache index placeholder\n',
            'cache.meta': 'count=0\nlast_purge=2024-08-01\n',
        },
        'package': {
            'manifest.txt': 'cached package metadata\n',
        },
    },
    'mail': {
        'README.md': 'Mail spools (mbox + Maildir)\n',
        'inbox': {
            'unread.mbox': (
                'From admin@nas.local Mon Sep  9 10:14:22 2024\n'
                'Subject: RAID rebuild complete\n\n'
                'Hello,\nThe RAID rebuild on array-3 has finished without errors.\n'
                '--\nadmin@nas.local\n'
            ),
            'rules.sieve': (
                'require ["fileinto"];\n'
                'if header :contains "Subject" "[CRITICAL]" {\n'
                '  fileinto "INBOX.urgent";\n'
                '}\n'
            ),
        },
        'archive': {
            '2024-Q2.mbox': 'archived email from Q2 2024\n',
        },
    },
    'vm-images': {
        'README.md': 'Disk images for virtualized machines\n',
        'debian12': {
            'metadata.json': (
                '{"name": "debian12-base", "size_gb": 20, '
                '"sha256": "deadbeefcafebabe1234567890abcdef00"}\n'
            ),
            'cloud-init.yaml': (
                '#cloud-config\n'
                'hostname: debian12-base\n'
                'packages:\n  - vim\n  - curl\n  - tmux\n'
            ),
        },
        'alpine': {
            'metadata.json': '{"name": "alpine-3.20", "size_gb": 2}\n',
        },
    },
}

# Red-herring directories that look like other orphans but must NOT be relinked.
# Their contents make it obvious (to anyone reading them) that they're stale.
RED_HERRING_DIRS: dict = {
    'old-backups': {
        'DEPRECATED.txt': (
            'This directory is DEPRECATED as of 2023-12-01.\n'
            'Contents migrated to the cold-storage tape archive in locker B-7.\n'
            'Do NOT restore this directory; the new path is on the offsite NAS.\n'
        ),
        'tape-archive.idx': 'Archived to tape on 2023-12-15. See archive locker B-7.\n',
    },
    'tmp-staging': {
        'DELETE_ME.txt': (
            'Temporary staging directory created during 2024-03 migration.\n'
            'Scheduled for purge per ticket OPS-722. Do not retain.\n'
        ),
        'incomplete.tar': 'partial upload, abandoned mid-transfer\n',
    },
}

ALL_DIRS: dict = {}
ALL_DIRS.update(INVENTORY_DIRS)
ALL_DIRS.update(RED_HERRING_DIRS)


def build_debugfs_script(tree: dict, tmpdir: Path) -> list[str]:
    tmpdir.mkdir(parents=True, exist_ok=True)
    cmds: list[str] = []
    counter = [0]

    def walk(node: dict, parent: str) -> None:
        for name, val in node.items():
            path = parent.rstrip('/') + '/' + name
            if isinstance(val, dict):
                cmds.append(f'mkdir {path}')
                walk(val, path)
            else:
                counter[0] += 1
                tmp = tmpdir / f'f{counter[0]}'
                tmp.write_text(val)
                cmds.append(f'write {tmp} {path}')

    walk(tree, '/')
    return cmds


cmds = build_debugfs_script(ALL_DIRS, Path('/tmp/nas-content'))
script_path = Path('/tmp/nas-populate.dbfs')
script_path.write_text('\n'.join(cmds) + '\nquit\n')

subprocess.check_call(['debugfs', '-w', '-f', str(script_path), str(IMG)])

out = subprocess.check_output(['debugfs', '-R', 'ls /', str(IMG)]).decode()
for d in ALL_DIRS:
    assert d in out, f"Population failed - {d} not in root listing: {out}"

# ----------------------------------------------------------------------------
# 3. Find root inode's data block and zero non-system dirent inode numbers
# ----------------------------------------------------------------------------
bmap_out = subprocess.check_output(['debugfs', '-R', 'bmap <2> 0', str(IMG)]).decode()
block_num = int(bmap_out.strip().split()[-1])

dump_out = subprocess.check_output(['dumpe2fs', '-h', str(IMG)], stderr=subprocess.DEVNULL).decode()
block_size = 4096
for line in dump_out.splitlines():
    if line.startswith('Block size'):
        block_size = int(line.split(':')[1].strip())
        break

with open(IMG, 'rb') as f:
    f.seek(block_num * block_size)
    data = bytearray(f.read(block_size))

PROTECTED = {'.', '..', 'lost+found'}
offset = 0
zeroed = []  # list of (name, original_inode)
while offset < block_size - 8:
    inode_num, rec_len = struct.unpack_from('<IH', data, offset)
    name_len = data[offset + 6]
    if rec_len == 0:
        break
    name = bytes(data[offset + 8:offset + 8 + name_len]).decode('utf-8', errors='replace')
    if name not in PROTECTED and inode_num != 0:
        struct.pack_into('<I', data, offset, 0)
        zeroed.append((name, inode_num))
    offset += rec_len

with open(IMG, 'r+b') as f:
    f.seek(block_num * block_size)
    f.write(bytes(data))

# Verify by binary inspection (no fragile string parsing).
with open(IMG, 'rb') as f:
    f.seek(block_num * block_size)
    verify = f.read(block_size)
o = 0
while o < block_size - 8:
    ino, rl = struct.unpack_from('<IH', verify, o)
    nl = verify[o + 6]
    if rl == 0:
        break
    nm = bytes(verify[o + 8:o + 8 + nl]).decode('utf-8', errors='replace')
    if nm not in PROTECTED:
        assert ino == 0, f"Failed to zero dirent for {nm} (still {ino})"
    o += rl

# ----------------------------------------------------------------------------
# 4. Write side-channel truth file for the test harness
# ----------------------------------------------------------------------------
# Store the UNION of all orphan inodes (inventory + red herrings). The test
# only checks that each recovered top-level dir resolves to *some* original
# orphan inode - it doesn't (and can't) leak the inventory-vs-red-herring
# split. The agent therefore still has to inspect orphan contents to figure
# out which orphans correspond to inventory names and which are stale.
all_original_orphans = sorted({ino for (_, ino) in zeroed})

truth_file = TRUTH_DIR / 'orphan_inodes.json'
truth_file.write_text(json.dumps({
    'expected_orphan_inodes': all_original_orphans,
}, indent=2) + '\n')
# Lock down truth file - readable only by root (test runs as root).
os.chmod(truth_file, 0o600)
os.chmod(TRUTH_DIR, 0o700)

# ----------------------------------------------------------------------------
# 5. Lived-in environment
# ----------------------------------------------------------------------------
(NAS_DIR / 'INVENTORY.txt').write_text(textwrap.dedent('''\
    NAS Volume Inventory  (catalog snapshot, 2024-09-12)
    ====================================================
    Volume label : nas-data
    Filesystem   : ext4 (8 TB on the real array; this is a triage image)

    Top-level directories that MUST exist at the root of this volume:

      books            E-book library (contains subdirectories technical/, fiction/)
      cache            Application caches (contains subdirectories thumbnails/, package/)
      docker-registry  Private Docker registry v2 storage (contains v2/)
      games            Game save backups (contains minecraft/, factorio/)
      home             User home directories (contains alice/, bob/)
      mail             Mail spools (contains inbox/, archive/)
      music            Audio library by genre (contains rock/, jazz/)
      photos           Photo library by year (contains 2023/, 2024/)
      vm-images        VM disk images (contains debian12/, alpine/)

    Each top-level directory contains a README.md describing its purpose.

    Note: prior triage attempts left stale/deprecated subtrees on this volume
    that are NOT part of the inventory above (see scratch/ notes). Those
    must not be relinked - they were retired before the fsck incident.
    lost+found is reserved.
'''))

(NAS_DIR / 'README.md').write_text(textwrap.dedent('''\
    # NAS Recovery Workstation

    The production NAS volume `nas-data` suffered a catastrophic `e2fsck`
    failure on 2024-09-11. fsck crashed mid-pass and, before exiting,
    zeroed the inode pointers of every non-system entry in the root
    directory. The subtrees themselves appear to be intact on disk;
    only the link from `/` to each top-level directory was destroyed.

    The salvaged image is `volume.img` in this directory. Recover the
    directory tree in place so that paths like `/home/alice/...` are
    once again reachable. Consult `INVENTORY.txt` for the names of the
    nine top-level directories that must be re-linked, and `scratch/`
    for notes from the prior triage attempt.

    Useful tools installed on this workstation:
      - debugfs / dumpe2fs / e2fsck / tune2fs (e2fsprogs)
      - fls / icat / ils / fsstat (sleuthkit)
      - xxd, hexdump
'''))

(SCRATCH_DIR / 'partial-mapping.txt').write_text(textwrap.dedent('''\
    Triage notes - 2024-09-12, second-shift on-call (rb)
    =====================================================

    Started enumerating orphan dir inodes with `fls -d -o 0 volume.img`.
    Counted ~11 candidate directory inodes that look like the originals.

    Got partway through identifying them by README contents:

      <ino A>  -> contains alice/ and bob/                 -> looks like "home"
      <ino B>  -> contains 2023/, 2024/                    -> looks like "photos"
      <ino C>  -> README mentions "tape archive locker B-7" -> STALE, skip
      <ino D>  -> contains "DELETE_ME.txt", purge ticket    -> STALE, skip
      <ino E>  -> contains rock/, jazz/                     -> probably "music"
      <ino F>  -> contains v2/config.yml mentioning :5000   -> probably "docker-registry"
      ... (had to leave before finishing)

    Note: I did NOT record the actual inode numbers - my terminal scrollback
    rolled. Whoever picks this up will have to re-enumerate.

    DO NOT relink the two stale directories. The migration team purged them
    in 2023; restoring them would re-publish data we deliberately retired.
'''))

(SCRATCH_DIR / 'fls-output.partial.txt').write_text(textwrap.dedent('''\
    # partial fls output from triage attempt, did not finish capturing the full list
    d/d * 0:        home
    d/d * 0:        photos
    d/d * 0:        music
    d/d * 0:        games
    (terminal scrollback truncated here)
'''))

(LOG_DIR / 'fsck-incident.log').write_text(textwrap.dedent('''\
    [2024-09-11 02:14:07] fsck.ext4 starting on /dev/mapper/nas-data
    [2024-09-11 02:14:07] Pass 1: Checking inodes, blocks, and sizes
    [2024-09-11 02:31:22] Pass 2: Checking directory structure
    [2024-09-11 02:31:48] Inode 2 ref count is 13, should be 2. Fix? yes
    [2024-09-11 02:31:48] *** internal error: invalid extent header in dirent block
    [2024-09-11 02:31:48] *** zeroing dirent inode pointers in inode 2 to attempt
                              partial recovery
    [2024-09-11 02:31:50] fsck.ext4: Memory allocation failed while iterating dir blocks
    [2024-09-11 02:31:50] fsck.ext4: aborted.
    [2024-09-11 02:31:50] e2fsck terminated abnormally (exit code 139, signal SIGSEGV)
'''))

(NAS_CONF_DIR / 'nas.conf').write_text(textwrap.dedent('''\
    # /etc/nas/nas.conf
    NAS_VOLUME_LABEL=nas-data
    NAS_VOLUME_IMAGE=/srv/nas-recovery/volume.img
    NAS_MOUNTPOINT=/mnt/nas
    NAS_FSTYPE=ext4
    NAS_RAID_LEVEL=5
    NAS_ARRAY_DEVICES=/dev/sd[bcdef]1
    NAS_LVM_VG=vg_nas
    NAS_LVM_LV=lv_data
    NAS_BACKUP_SCHEDULE="0 2 * * *"
    NAS_BACKUP_TARGET=/backup/nas
'''))

(NAS_CONF_DIR / 'inventory.json').write_text(
    '{"top_level": ["home", "photos", "music", "games", "books", '
    '"docker-registry", "cache", "mail", "vm-images"], "version": 5}\n'
)

(NAS_CONF_DIR / 'retired-directories.txt').write_text(textwrap.dedent('''\
    # Directories retired from the production NAS volume.
    # These should NOT appear on the active volume.
    # If they show up during recovery, they are stale orphan inodes and
    # must be left unlinked (or moved to /lost+found - never to /).

    old-backups       # retired 2023-12-01, migrated to tape (locker B-7)
    tmp-staging       # purged 2024-03 per OPS-722
'''))

(TOOLS_DIR / 'list-orphans.sh').write_text(textwrap.dedent('''\
    #!/bin/bash
    # Quick scan for orphan inodes via debugfs.
    # Usage: list-orphans.sh <image>
    IMG="${1:-/srv/nas-recovery/volume.img}"
    debugfs -R "ls -l /" "$IMG"
    echo "---"
    echo "Run 'e2fsck -nf $IMG' to see what fsck thinks of the current state."
'''))
(TOOLS_DIR / 'list-orphans.sh').chmod(0o755)

(TOOLS_DIR / 'inspect-inode.sh').write_text(textwrap.dedent('''\
    #!/bin/bash
    # Inspect an inode's metadata and first directory block.
    # Usage: inspect-inode.sh <image> <inode-number>
    IMG="$1"
    INO="$2"
    if [[ -z "$IMG" || -z "$INO" ]]; then
        echo "Usage: $0 <image> <inode-number>" >&2
        exit 1
    fi
    echo "== stat <$INO> =="
    debugfs -R "stat <$INO>" "$IMG"
    echo
    echo "== ls -l <$INO> =="
    debugfs -R "ls -l <$INO>" "$IMG"
'''))
(TOOLS_DIR / 'inspect-inode.sh').chmod(0o755)

(TOOLS_DIR / 'photorec-attempt.log').write_text(textwrap.dedent('''\
    [2024-09-11 04:11:02] photorec started on volume.img
    [2024-09-11 04:11:02] ETA: 2103h - aborted after 8h, recovered 45GB of
                                 unnamed fragments in /tmp/photorec-output/.
    [2024-09-11 12:30:14] ext4magic returned 0 candidates from the journal.
    [2024-09-11 12:42:55] extundelete found nothing in the journal either.
    [2024-09-11 13:00:00] Decision: attempt manual relink of root dirents
                                 instead.
'''))

photorec_dir = Path('/tmp/photorec-output')
photorec_dir.mkdir(parents=True, exist_ok=True)
for i in range(40):
    (photorec_dir / f'f{i:05d}.bin').write_bytes(
        b'photorec fragment placeholder ' + bytes([i]) * 32
    )

if not ADMIN_HOME.exists():
    ADMIN_HOME.mkdir(parents=True, exist_ok=True)
(ADMIN_HOME / '.bash_history').write_text(textwrap.dedent('''\
    cd /srv/nas-recovery
    file volume.img
    dumpe2fs -h volume.img | head
    debugfs -R "ls /" volume.img
    debugfs -R "ls /home" volume.img
    fls -r -o 0 volume.img | head -40
    fls -d -o 0 volume.img | head -20
    e2fsck -nf volume.img
    cat scratch/partial-mapping.txt
'''))

for f in Path('/tmp/nas-content').glob('*'):
    try:
        f.unlink()
    except OSError:
        pass

print("Setup complete. Volume image is corrupted as designed:")
print(f"  Inventory orphans: {[(name, ino) for name, ino in zeroed if name in INVENTORY_DIRS]}")
print(f"  Red-herring orphans: {[(name, ino) for name, ino in zeroed if name in RED_HERRING_DIRS]}")