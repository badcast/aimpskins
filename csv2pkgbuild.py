#!/usr/bin/env python3
"""
csv2pkgbuild.py - generate an AUR PKGBUILD (and optionally .SRCINFO) for AIMP
skins from a CSV file.

CSV format (header row required):
    number,name,version,author,link,sha256

Modes:
    split    (default) one package per skin: aimp-skin-<name>
    single   one package "aimp-skins" that installs every skin
    parallel each skin gets its own subdirectory aimp-skins/aimp-skin-<slug>/
             with a standalone PKGBUILD and .SRCINFO; all packages belong to
             the AIMP_Skins group

Examples:
    ./csv2pkgbuild.py aimp_skins.csv
    ./csv2pkgbuild.py aimp_skins.csv -o PKGBUILD --mode single --srcinfo
    ./csv2pkgbuild.py aimp_skins.csv --mode parallel
    ./csv2pkgbuild.py aimp_skins.csv --mode parallel --publish
    ./csv2pkgbuild.py aimp_skins.csv --mode parallel --refresh
    ./csv2pkgbuild.py aimp_skins.csv --mode parallel --refresh --write -j 8
    ./csv2pkgbuild.py aimp_skins.csv --mode parallel --clone -j 8
    ./csv2pkgbuild.py --clone -j 8
    ./csv2pkgbuild.py --check -j 8
    ./csv2pkgbuild.py aimp_skins.csv --mode parallel --init -j 8
    ./csv2pkgbuild.py aimp_skins.csv --mode parallel --publish -j 8
    ./csv2pkgbuild.py --cmd "git add -A ." -j 8
    ./csv2pkgbuild.py --cmd "git status --short" -j
    ./csv2pkgbuild.py aimp_skins.csv --skins-dir /usr/share/aimp/Skins \\
        --depends aimp wine --maintainer "Jane Doe <jane@example.com>"

Note: `makepkg` needs roughly 0.4 s per split sub-package for *every* run
(including `--printsrcinfo`), so use --srcinfo instead of makepkg to produce
.SRCINFO for large split PKGBUILDs, or prefer --mode single.
"""

import argparse
import csv
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date

print_lock = threading.Lock()


def get_git_env() -> dict:
    """Return environment with SSH multiplexing enabled for aur.archlinux.org.

    Sharing one master SSH connection prevents AUR sshd from throttling
    or dropping rapid concurrent connections (MaxStartups / rate limiting).
    """
    env = os.environ.copy()
    if 'GIT_SSH_COMMAND' not in env:
        sock = f"/tmp/aur-ssh-{os.getuid()}-%r@%h:%p"
        ssh_cmd = (
            f"ssh -o ControlMaster=auto -o ControlPath={sock} -o ControlPersist=300s "
            "-o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ConnectTimeout=15"
        )
        env['GIT_SSH_COMMAND'] = ssh_cmd
    return env

PKGBASE = 'aimp-skins'
PKGDESC = 'Skins for AIMP audio player (collection from aimp.ru)'
DEFAULT_URL = 'https://github.com/badcast/aimpskins'
DEFAULT_SKINS_DIR = '/opt/aimp/Skins'
DEFAULT_MAINTAINER = 'badcast <lmecomposer@gmail.com>'
SPLIT_WARN_THRESHOLD = 50  # warn when a split PKGBUILD has more sub-packages
PARALLEL_GROUP = 'AIMP_Skins'
AUR_REMOTE_TPL = 'ssh://aur@aur.archlinux.org/{pkg}.git'
AUR_HTTPS_REMOTE_TPL = 'https://aur.archlinux.org/{pkg}.git'

TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}

REQUIRED_COLUMNS = ('name', 'link')


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def translit(text: str) -> str:
    """Transliterate Cyrillic to Latin and drop any remaining non-ASCII."""
    text = ''.join(TRANSLIT.get(ch.lower(), ch) for ch in text)
    return unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()


def slugify(name: str) -> str:
    """Make a valid, lowercase ASCII package-name fragment."""
    s = translit(name).lower().replace('&', ' and ')
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s or 'skin'


def fs_safe(name: str) -> str:
    """Make a safe directory name for the skin inside the Skins folder."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip(' .')
    return s or 'skin'


def sq(text) -> str:
    """Quote a value for bash using single quotes."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def desc(skin: dict) -> str:
    version = f" {skin['version']}" if skin['version'] else ''
    author = f" (by {skin['author']})" if skin['author'] else ''
    return f"AIMP skin: {skin['name']}{version}{author}"


# --------------------------------------------------------------------------- #
# CSV -> skin records
# --------------------------------------------------------------------------- #

def read_rows(path: str) -> list:
    rows = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            sys.exit(f"CSV is missing required column(s): {', '.join(missing)}")
        for r in reader:
            link = (r.get('link') or '').strip()
            m = re.search(r'[?&]id=(\d+)', link)
            if not link or not m:
                print(f"Skipping row (no link or id): {r}", file=sys.stderr)
                continue
            rows.append({
                'number': (r.get('number') or '').strip(),
                'name': (r.get('name') or '').strip(),
                'version': (r.get('version') or '').strip(),
                'author': (r.get('author') or '').strip(),
                'link': link,
                'id': m.group(1),
                'sha256': (r.get('sha256') or '').strip(),
            })
    return rows


def write_csv(path: str, skins: list) -> None:
    """Write updated skin records back to CSV, preserving columns."""
    fieldnames = ['number', 'name', 'version', 'author', 'link', 'sha256']
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for i, s in enumerate(skins, 1):
            writer.writerow({
                'number': s.get('number') or str(i),
                'name': s.get('name', ''),
                'version': s.get('version', ''),
                'author': s.get('author', ''),
                'link': s.get('link', ''),
                'sha256': s.get('sha256', ''),
            })


def prepare(rows: list) -> list:
    """Add unique package names, directory names and source file names."""
    used_pkg, used_dir, skins = set(), set(), []
    for r in rows:
        pkg = f"aimp-skin-{slugify(r['name'])}"
        if pkg in used_pkg:
            pkg = f"{pkg}-{r['id']}"
        used_pkg.add(pkg)

        d = fs_safe(r['name'])
        if d.lower() in used_dir:
            d = f"{d} ({r['id']})"
        used_dir.add(d.lower())

        skins.append({**r, 'pkg': pkg, 'dir': d, 'file': f"{pkg}-{r['id']}.archive"})
    return skins


# --------------------------------------------------------------------------- #
# PKGBUILD
# --------------------------------------------------------------------------- #

def header(args, skins) -> list:
    lines = []
    if args.maintainer:
        lines.append(f"# Maintainer: {args.maintainer}")
    lines.append(f"# Generated by csv2pkgbuild.py - {len(skins)} skins - {args.url}")
    lines.append("")
    return lines


def common_tail(args, skins) -> list:
    L = [
        f"pkgver={args.pkgver}",
        f"pkgrel={args.pkgrel}",
        "arch=('any')",
        f"url={sq(args.url)}",
        "license=('custom')",
        f"depends=({' '.join(sq(d) for d in args.depends)})",
        "makedepends=('libarchive')",
        "options=('!strip' '!debug')",
        f"_skinsdir={sq(args.skins_dir)}",
        "",
        "# The links are redirects (?do=catalog.download&id=N), so the local file",
        "# name is given explicitly (name::url). Refresh checksums with: updpkgsums",
        "source=(",
    ]
    for s in skins:
        L.append(f"  {sq(s['file'] + '::' + s['link'])}")
    L += [
        ")",
        "",
        "# Everything is extracted manually in package(); makepkg must not touch it",
        'noextract=("${source[@]%%::*}")',
        "",
        "sha256sums=(",
    ]
    L += [f"  {sq(s['sha256'] or 'SKIP')}" for s in skins]
    L += [
        ")",
        "",
        "# _install_skin <source-file>",
        "_install_skin() {",
        '  local dest="$pkgdir$_skinsdir"',
        '  install -dm755 "$dest"',
        '  bsdtar -xf "$srcdir/$1" -C "$dest"',
        '  find "$dest" -mindepth 2 -type f -exec mv -t "$dest" {} +',
        '  find "$dest" -mindepth 1 -type d -empty -delete',
        '  find "$dest" -type d -exec chmod 755 {} +',
        '  find "$dest" -type f -exec chmod 644 {} +',
        "}",
        "",
    ]
    return L


def build_split(args, skins) -> list:
    L = header(args, skins)
    L.append(f"pkgbase={PKGBASE}")
    L.append("pkgname=(")
    L += [f"  {sq(s['pkg'])}" for s in skins]
    L.append(")")
    L.append(f"pkgdesc={sq(PKGDESC)}")
    L += common_tail(args, skins)
    for s in skins:
        L.append(f"package_{s['pkg']}() {{")
        L.append(f"  pkgdesc={sq(desc(s))}")
        L.append(f"  _install_skin {sq(s['file'])}")
        L.append("}")
        L.append("")
    return L


def build_single(args, skins) -> list:
    L = header(args, skins)
    L.append(f"pkgname={PKGBASE}")
    L.append(f"pkgdesc={sq(PKGDESC)}")
    L += common_tail(args, skins)
    L.append("# format: 'source-file'   # description")
    L.append("_skins=(")
    for s in skins:
        L.append(f"  {sq(s['file'])}  # {desc(s).replace(chr(10), ' ')}")
    L.append(")")
    L.append("")
    L.append("package() {")
    L.append("  local file")
    L.append('  for file in "${_skins[@]}"; do')
    L.append('    _install_skin "$file"')
    L.append("  done")
    L.append("}")
    L.append("")
    return L


# --------------------------------------------------------------------------- #
# parallel mode — one directory per skin, each with its own PKGBUILD/.SRCINFO
# --------------------------------------------------------------------------- #

def build_parallel_pkgbuild(args, skin) -> list:
    """Generate a standalone PKGBUILD for a single skin."""
    L = []
    if args.maintainer:
        L.append(f"# Maintainer: {args.maintainer}")
    L.append(f"# Generated by csv2pkgbuild.py - {args.url}")
    L.append("")
    L.append(f"pkgname={sq(skin['pkg'])}")
    L.append(f"pkgdesc={sq(desc(skin))}")
    L.append(f"pkgver={args.pkgver}")
    L.append(f"pkgrel={args.pkgrel}")
    L.append("arch=('any')")
    L.append(f"url={sq(args.url)}")
    L.append("license=('custom')")
    L.append(f"groups=({sq(PARALLEL_GROUP)})")
    L.append(f"depends=({' '.join(sq(d) for d in args.depends)})")
    L.append("makedepends=('libarchive')")
    L.append("options=('!strip' '!debug')")
    L.append(f"_skinsdir={sq(args.skins_dir)}")
    L.append("")
    L.append(f"source=({sq(skin['file'] + '::' + skin['link'])})")
    L.append('noextract=("${source[@]%%::*}")')
    L.append(f"sha256sums=({sq(skin['sha256'] or 'SKIP')})")
    L.append("")
    L.append("package() {")
    L.append('  local dest="$pkgdir$_skinsdir"')
    L.append('  install -dm755 "$dest"')
    L.append(f'  bsdtar -xf "$srcdir/{skin["file"]}" -C "$dest"')
    L.append('  find "$dest" -mindepth 2 -type f -exec mv -t "$dest" {} +')
    L.append('  find "$dest" -mindepth 1 -type d -empty -delete')
    L.append('  find "$dest" -type d -exec chmod 755 {} +')
    L.append('  find "$dest" -type f -exec chmod 644 {} +')
    L.append("}")
    L.append("")
    return L


def build_parallel_srcinfo(args, skin) -> list:
    """Generate .SRCINFO for a single skin package."""
    T = "\t"
    L = [f"pkgbase = {skin['pkg']}"]
    L.append(f"{T}pkgdesc = {desc(skin)}")
    L.append(f"{T}pkgver = {args.pkgver}")
    L.append(f"{T}pkgrel = {args.pkgrel}")
    L.append(f"{T}url = {args.url}")
    L.append(f"{T}arch = any")
    L.append(f"{T}license = custom")
    L.append(f"{T}groups = {PARALLEL_GROUP}")
    L.append(f"{T}makedepends = libarchive")
    L += [f"{T}depends = {d}" for d in args.depends]
    L.append(f"{T}noextract = {skin['file']}")
    L.append(f"{T}options = !strip")
    L.append(f"{T}options = !debug")
    L.append(f"{T}source = {skin['file']}::{skin['link']}")
    L.append(f"{T}sha256sums = {skin['sha256'] or 'SKIP'}")
    L.append("")
    L.append(f"pkgname = {skin['pkg']}")
    L.append("")
    return L


# --------------------------------------------------------------------------- #
# .SRCINFO
# --------------------------------------------------------------------------- #

def build_srcinfo(args, skins) -> list:
    """Equivalent of `makepkg --printsrcinfo`, but instant for huge PKGBUILDs."""
    T = "\t"
    L = [f"pkgbase = {PKGBASE}"]
    L.append(f"{T}pkgdesc = {PKGDESC}")
    L.append(f"{T}pkgver = {args.pkgver}")
    L.append(f"{T}pkgrel = {args.pkgrel}")
    L.append(f"{T}url = {args.url}")
    L.append(f"{T}arch = any")
    L.append(f"{T}license = custom")
    L.append(f"{T}makedepends = libarchive")
    L += [f"{T}depends = {d}" for d in args.depends]
    L += [f"{T}noextract = {s['file']}" for s in skins]
    L.append(f"{T}options = !strip")
    L.append(f"{T}options = !debug")
    L += [f"{T}source = {s['file']}::{s['link']}" for s in skins]
    L += [f"{T}sha256sums = {s['sha256'] or 'SKIP'}" for s in skins]
    L.append("")
    if args.mode == 'split':
        for s in skins:
            L.append(f"pkgname = {s['pkg']}")
            L.append(f"{T}pkgdesc = {desc(s)}")
            L.append("")
    else:
        L.append(f"pkgname = {PKGBASE}")
        L.append("")
    return L


# --------------------------------------------------------------------------- #
# AUR git operations (parallel mode)
# --------------------------------------------------------------------------- #

def _git(args: list, cwd: str, check: bool = True):
    """Run a git command in *cwd* and return the CompletedProcess."""
    return subprocess.run(
        ['git'] + args, cwd=cwd, check=check,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=get_git_env(),
    )


def aur_clone(base_dir: str, skins: list, jobs: int = 1) -> None:
    """Clone every parallel package from the AUR via HTTPS.

    For each skin: ``git clone https://aur.archlinux.org/<pkg>.git``
    into *base_dir*/<pkg>/. Skips directories that already contain a
    ``.git`` folder.

    To protect against AUR rate limiting (Connection reset / TLS unexpected EOF),
    concurrency is capped at 5, HTTP/1.1 is forced, and exponential backoff
    with random jitter is used for retries (up to 6 attempts).
    """
    os.makedirs(base_dir, exist_ok=True)
    stats = {'ok': 0, 'empty': 0, 'skip': 0, 'fail': 0}
    max_attempts = 6

    # Cap concurrent network connections to aur.archlinux.org to avoid DDoS / rate-limiting
    effective_jobs = min(jobs, 5) if jobs > 1 else 1
    if jobs > 5:
        print(f"Limiting concurrent HTTPS clones to {effective_jobs} "
              f"to prevent AUR rate-limiting / connection resets.")

    def _clone(skin):
        pkg = skin['pkg']
        skin_dir = os.path.join(base_dir, pkg)
        git_dir = os.path.join(skin_dir, '.git')

        if os.path.isdir(git_dir):
            with print_lock:
                print(f"  SKIP {pkg} (already cloned)")
            return 'skip'

        remote_url = AUR_HTTPS_REMOTE_TPL.format(pkg=pkg)

        # Break initial lockstep synchronization among threads
        time.sleep(random.uniform(0.05, 0.3))

        for attempt in range(1, max_attempts + 1):
            # If directory exists and is not empty, git clone will error.
            # Handle non-empty directory by git init + fetch.
            if os.path.isdir(skin_dir) and os.listdir(skin_dir):
                _git(['init'], cwd=skin_dir)
                r_orig = _git(['remote', 'get-url', 'origin'], cwd=skin_dir, check=False)
                if r_orig.returncode == 0:
                    _git(['remote', 'set-url', 'origin', remote_url], cwd=skin_dir)
                else:
                    _git(['remote', 'add', 'origin', remote_url], cwd=skin_dir)
                r = _git(['-c', 'http.version=HTTP/1.1', 'fetch', 'origin', 'master'], cwd=skin_dir, check=False)
                if r.returncode == 0:
                    _git(['checkout', '-B', 'master', '-f', 'origin/master'], cwd=skin_dir, check=False)
                    with print_lock:
                        print(f"  ATTACH {pkg} (fetched from AUR via HTTPS)")
                    return 'ok'
                elif attempt == max_attempts:
                    with print_lock:
                        print(f"  INIT {pkg} (new repo on AUR)")
                    return 'empty'

            # Clean any incomplete clone before attempting
            if os.path.isdir(skin_dir) and not os.path.isfile(os.path.join(skin_dir, 'PKGBUILD')):
                shutil.rmtree(skin_dir, ignore_errors=True)

            r = subprocess.run(
                [
                    'git',
                    '-c', 'http.version=HTTP/1.1',
                    '-c', 'http.lowSpeedLimit=1000',
                    '-c', 'http.lowSpeedTime=30',
                    'clone', remote_url, skin_dir,
                ],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if r.returncode == 0:
                r_head = subprocess.run(
                    ['git', 'rev-parse', 'HEAD'],
                    cwd=skin_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                if r_head.returncode == 0:
                    with print_lock:
                        print(f"  CLONE {pkg}")
                    return 'ok'
                else:
                    with print_lock:
                        print(f"  CLONE {pkg} (empty repo on AUR)")
                    return 'empty'

            err_line = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'unknown error'
            if attempt < max_attempts:
                # Exponential backoff with random jitter: 2^attempt + jitter
                delay = min(25.0, (2.0 ** attempt) + random.uniform(1.0, 3.5))
                with print_lock:
                    print(f"  RETRY {pkg} (attempt {attempt}/{max_attempts}): "
                          f"AUR connection dropped, waiting {delay:.1f}s...")
                time.sleep(delay)

        with print_lock:
            print(f"  FAIL {pkg}: {err_line}", file=sys.stderr)
        return 'fail'

    if effective_jobs > 1:
        with ThreadPoolExecutor(max_workers=effective_jobs) as pool:
            for res in pool.map(_clone, skins):
                stats[res] += 1
    else:
        for skin in skins:
            stats[_clone(skin)] += 1

    print(f"Clone done: {stats['ok']} cloned, {stats['empty']} empty on AUR, "
          f"{stats['skip']} skipped, {stats['fail']} failed, {len(skins)} total")


def aur_init(base_dir: str, skins: list, jobs: int = 1) -> None:
    """Clone or attach every parallel package from the AUR.

    For each skin: ``git clone ssh://aur@aur.archlinux.org/<pkg>.git``
    into *base_dir*/<pkg>/. Skips directories that already contain a
    ``.git`` folder.
    """
    os.makedirs(base_dir, exist_ok=True)
    stats = {'ok': 0, 'skip': 0, 'fail': 0}

    def _clone(skin):
        pkg = skin['pkg']
        skin_dir = os.path.join(base_dir, pkg)
        git_dir = os.path.join(skin_dir, '.git')

        if os.path.isdir(git_dir):
            with print_lock:
                print(f"  SKIP {pkg} (already cloned)")
            return 'skip'

        remote_url = AUR_REMOTE_TPL.format(pkg=pkg)
        env = get_git_env()

        for attempt in range(1, 6):
            # If directory exists and is not empty, git clone will error.
            # Handle non-empty directory by git init + fetch.
            if os.path.isdir(skin_dir) and os.listdir(skin_dir):
                _git(['init'], cwd=skin_dir)
                r_orig = _git(['remote', 'get-url', 'origin'], cwd=skin_dir, check=False)
                if r_orig.returncode == 0:
                    _git(['remote', 'set-url', 'origin', remote_url], cwd=skin_dir)
                else:
                    _git(['remote', 'add', 'origin', remote_url], cwd=skin_dir)
                r = _git(['fetch', 'origin', 'master'], cwd=skin_dir, check=False)
                if r.returncode == 0:
                    _git(['checkout', '-B', 'master', '-f', 'origin/master'], cwd=skin_dir, check=False)
                    with print_lock:
                        print(f"  ATTACH {pkg} (fetched from AUR)")
                    return 'ok'
                elif attempt == 5:
                    with print_lock:
                        print(f"  INIT {pkg} (new repo on AUR)")
                    return 'ok'

            # Clean any incomplete clone before attempting
            if os.path.isdir(skin_dir) and not os.path.isfile(os.path.join(skin_dir, 'PKGBUILD')):
                shutil.rmtree(skin_dir, ignore_errors=True)

            r = subprocess.run(
                ['git', 'clone', remote_url, skin_dir],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=env,
            )
            if r.returncode == 0:
                with print_lock:
                    print(f"  CLONE {pkg}")
                return 'ok'

            err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'unknown error'
            if attempt < 5:
                delay = min(20.0, (2.0 ** attempt) + random.uniform(0.5, 2.0))
                with print_lock:
                    print(f"  RETRY {pkg} (attempt {attempt}/5): SSH connection dropped, waiting {delay:.1f}s...")
                time.sleep(delay)

        with print_lock:
            print(f"  FAIL {pkg}: {err}", file=sys.stderr)
        return 'fail'

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for res in pool.map(_clone, skins):
                stats[res] += 1
    else:
        for skin in skins:
            stats[_clone(skin)] += 1

    print(f"Init done: {stats['ok']} cloned/attached, {stats['skip']} skipped, {stats['fail']} failed, {len(skins)} total")


def aur_publish(base_dir: str, skins: list, jobs: int = 1) -> None:
    """Commit and push every parallel package directory to the AUR.

    For each skin directory under *base_dir*:
      1. If it is not a git repo yet — `git init` + set remote to the AUR.
      2. Stage PKGBUILD and .SRCINFO.
      3. Commit (skip if tree is clean).
      4. Push to the AUR remote.
    """
    stats = {'ok': 0, 'fail': 0}

    def _publish(skin):
        pkg = skin['pkg']
        skin_dir = os.path.join(base_dir, pkg)
        if not os.path.isdir(skin_dir):
            with print_lock:
                print(f"  SKIP {pkg} (directory missing)", file=sys.stderr)
            return 'fail'

        git_dir = os.path.join(skin_dir, '.git')
        remote_url = AUR_REMOTE_TPL.format(pkg=pkg)

        # --- init repo if needed ---
        if not os.path.isdir(git_dir):
            _git(['init'], cwd=skin_dir)
            _git(['remote', 'add', 'origin', remote_url], cwd=skin_dir)
            with print_lock:
                print(f"  INIT {pkg} -> {remote_url}")
        else:
            # Ensure remote 'origin' points to the AUR
            r = _git(['remote', 'get-url', 'origin'], cwd=skin_dir, check=False)
            if r.returncode != 0:
                _git(['remote', 'add', 'origin', remote_url], cwd=skin_dir)
            elif r.stdout.strip() != remote_url:
                _git(['remote', 'set-url', 'origin', remote_url], cwd=skin_dir)

        # --- stage + commit ---
        _git(['add', 'PKGBUILD', '.SRCINFO'], cwd=skin_dir)

        status = _git(['status', '--porcelain'], cwd=skin_dir)
        if not status.stdout.strip():
            with print_lock:
                print(f"  OK   {pkg} (nothing to commit)")
            return 'ok'

        _git(['commit', '-m', f'Update {pkg} to {skin.get("version", "")} '
              f'(pkgver auto-generated)'.rstrip()], cwd=skin_dir)

        # --- push ---
        r = _git(['push', 'origin', 'master'], cwd=skin_dir, check=False)
        if r.returncode != 0:
            # first push — the AUR branch may not exist yet
            r = _git(['push', '-u', 'origin', 'master'], cwd=skin_dir, check=False)
        if r.returncode != 0:
            with print_lock:
                print(f"  FAIL {pkg}: {r.stderr.strip()}", file=sys.stderr)
            return 'fail'
        else:
            with print_lock:
                print(f"  PUSH {pkg}")
            return 'ok'

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for res in pool.map(_publish, skins):
                stats[res] += 1
    else:
        for skin in skins:
            stats[_publish(skin)] += 1

    print(f"Publish done: {stats['ok']} ok, {stats['fail']} failed, {len(skins)} total")


def aur_refresh(base_dir: str, skins: list, args = None, jobs: int = 1) -> None:
    """Run ``git pull``, execute ``updpkgsums`` in every package directory, and update sha256 sums."""
    if shutil.which('updpkgsums') is None:
        sys.exit("updpkgsums command not found. Please install pacman-contrib.")

    stats = {'ok': 0, 'fail': 0, 'updated_sha': 0}

    def _refresh(skin):
        pkg = skin['pkg']
        skin_dir = os.path.join(base_dir, pkg)
        git_dir = os.path.join(skin_dir, '.git')
        if not os.path.isdir(git_dir):
            with print_lock:
                print(f"  SKIP {pkg} (not a git repo)", file=sys.stderr)
            return 'fail', False

        r = _git(['pull', '--ff-only'], cwd=skin_dir, check=False)
        pull_summary = r.stdout.strip().split('\n')[-1] if r.stdout.strip() else 'ok'
        if r.returncode != 0:
            with print_lock:
                print(f"  FAIL {pkg} (pull): {r.stderr.strip()}", file=sys.stderr)
            return 'fail', False

        pkgbuild_path = os.path.join(skin_dir, 'PKGBUILD')
        srcinfo_path = os.path.join(skin_dir, '.SRCINFO')

        # Ensure PKGBUILD exists
        if not os.path.isfile(pkgbuild_path) and args:
            write_lines(pkgbuild_path, build_parallel_pkgbuild(args, skin))
            write_lines(srcinfo_path, build_parallel_srcinfo(args, skin))

        # Run updpkgsums in skin_dir
        r_sums = subprocess.run(
            ['updpkgsums', 'PKGBUILD'],
            cwd=skin_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        # Clean up any leftover downloaded archive / src/ from updpkgsums
        for item in os.listdir(skin_dir):
            if item.endswith('.archive'):
                try:
                    os.remove(os.path.join(skin_dir, item))
                except OSError:
                    pass
            elif item == 'src':
                shutil.rmtree(os.path.join(skin_dir, item), ignore_errors=True)

        if r_sums.returncode != 0:
            with print_lock:
                err = r_sums.stderr.strip().splitlines()[-1] if r_sums.stderr.strip() else 'error'
                print(f"  FAIL {pkg} (updpkgsums): {err}", file=sys.stderr)
            return 'fail', False

        # Read updated sha256sums from PKGBUILD
        new_sha = None
        if os.path.isfile(pkgbuild_path):
            with open(pkgbuild_path, 'r', encoding='utf-8') as f:
                content = f.read()
            m = re.search(r"sha256sums\s*=\s*\(\s*['\"]([^'\"]*)['\"]\s*\)", content)
            if m:
                new_sha = m.group(1).strip()

        sha_changed = False
        if new_sha and new_sha != 'SKIP':
            old_sha = skin.get('sha256', '')
            if new_sha != old_sha:
                skin['sha256'] = new_sha
                sha_changed = True

            # Also update .SRCINFO to match the new sha256
            if os.path.isfile(srcinfo_path):
                with open(srcinfo_path, 'r', encoding='utf-8') as f:
                    scontent = f.read()
                new_scontent = re.sub(r"(\t?sha256sums\s*=\s*)[^\r\n]+", rf"\g<1>{new_sha}", scontent)
                if new_scontent != scontent:
                    with open(srcinfo_path, 'w', encoding='utf-8', newline='\n') as f:
                        f.write(new_scontent)

        with print_lock:
            msg = f"  REFRESH {pkg}: pull={pull_summary}"
            if new_sha:
                if sha_changed:
                    msg += f" (sha256 updated -> {new_sha[:10]}...)"
                else:
                    msg += f" (sha256 verified)"
            print(msg)

        return 'ok', sha_changed

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for status, sha_upd in pool.map(_refresh, skins):
                stats[status] += 1
                if sha_upd:
                    stats['updated_sha'] += 1
    else:
        for skin in skins:
            status, sha_upd = _refresh(skin)
            stats[status] += 1
            if sha_upd:
                stats['updated_sha'] += 1

    print(f"Refresh done: {stats['ok']} ok ({stats['updated_sha']} sha256 updated), "
          f"{stats['fail']} failed, {len(skins)} total")

    if args and getattr(args, 'write', False):
        write_csv(args.csv, skins)
        print(f"Updated CSV: saved new sha256 sums to {args.csv}")
    elif stats['updated_sha'] > 0 and args:
        print(f"Note: {stats['updated_sha']} sha256 sums were updated. "
              f"Run with --write to save them back into {args.csv}.")


# --------------------------------------------------------------------------- #
# Generic command execution (parallel directories)
# --------------------------------------------------------------------------- #

def run_cmd_each(base_dir: str, command: str, jobs: int = 1) -> None:
    """Run *command* (via the shell) in every subdirectory of *base_dir*."""
    subdirs = sorted(
        d for d in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, d))
    )
    if not subdirs:
        sys.exit(f"No subdirectories found in {base_dir}/")

    stats = {'ok': 0, 'fail': 0}

    def _run_one(name):
        path = os.path.join(base_dir, name)
        r = subprocess.run(command, shell=True, cwd=path,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
        with print_lock:
            print(f"  [{name}] $ {command}")
            if r.stdout.strip():
                for line in r.stdout.strip().splitlines():
                    print(f"    {line}")
            if r.returncode != 0:
                print(f"    -> exit {r.returncode}", file=sys.stderr)
        return 'ok' if r.returncode == 0 else 'fail'

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            for res in pool.map(_run_one, subdirs):
                stats[res] += 1
    else:
        for name in subdirs:
            stats[_run_one(name)] += 1

    print(f"Command done: {stats['ok']} ok, {stats['fail']} failed, {len(subdirs)} total")


def check_skins_status(base_dir: str, skins: list = None, jobs: int = 1) -> None:
    """Check publication status of every package in base_dir.

    Outputs lists of packages that are published (pushed and up-to-date with AUR)
    and not yet published (unpushed commits, uncommitted changes, or missing).
    """
    if skins:
        pkg_names = [s['pkg'] for s in skins]
    else:
        pkg_names = sorted(
            d for d in os.listdir(base_dir)
            if os.path.isdir(os.path.join(base_dir, d))
        )

    if not pkg_names:
        sys.exit(f"No package directories found in {base_dir}/")

    def _check_one(pkg):
        skin_dir = os.path.join(base_dir, pkg)
        if not os.path.isdir(skin_dir):
            return pkg, False, 'directory does not exist'

        git_dir = os.path.join(skin_dir, '.git')
        if not os.path.isdir(git_dir):
            return pkg, False, 'not a git repository (missing .git)'

        r_head = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=skin_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if r_head.returncode != 0:
            return pkg, False, 'no commits yet'
        head_sha = r_head.stdout.strip()

        r_status = subprocess.run(
            ['git', 'status', '--porcelain'],
            cwd=skin_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if r_status.stdout.strip():
            return pkg, False, 'has uncommitted changes'

        r_origin = subprocess.run(
            ['git', 'rev-parse', 'origin/master'],
            cwd=skin_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if r_origin.returncode != 0:
            return pkg, False, 'remote origin/master not found'
        origin_sha = r_origin.stdout.strip()

        if head_sha != origin_sha:
            r_ahead = subprocess.run(
                ['git', 'rev-list', 'origin/master..HEAD'],
                cwd=skin_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            count = len(r_ahead.stdout.strip().splitlines()) if r_ahead.stdout.strip() else 0
            if count > 0:
                return pkg, False, f'{count} unpushed commit(s) ahead of origin/master'
            else:
                return pkg, False, 'behind origin/master'

        return pkg, True, 'up-to-date with AUR'

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(_check_one, pkg_names))
    else:
        results = [_check_one(pkg) for pkg in pkg_names]

    published = [r for r in results if r[1]]
    unpushed = [r for r in results if not r[1]]

    print(f"\n=== Отправленные в AUR (Published): {len(published)} ===")
    if published:
        for pkg, _, _ in published:
            print(f"  [✓] {pkg}")
    else:
        print("  (нет)")

    print(f"\n=== Еще не отправленные в AUR (Not published): {len(unpushed)} ===")
    if unpushed:
        for pkg, _, reason in unpushed:
            print(f"  [✗] {pkg} ({reason})")
    else:
        print("  (нет)")

    print(f"\nИтого: {len(published)} отправлено, {len(unpushed)} не отправлено (всего: {len(pkg_names)})\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def write_lines(path: str, lines: list) -> None:
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines).rstrip('\n') + '\n')


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate an AUR PKGBUILD for AIMP skins from a CSV file.")
    ap.add_argument('csv', nargs='?', default=None,
                    help='input CSV (number,name,version,author,link,sha256)')
    ap.add_argument('-o', '--output', default='PKGBUILD',
                    help='output PKGBUILD file (default: PKGBUILD)')
    ap.add_argument('--mode', choices=['split', 'single', 'parallel'],
                    default='split',
                    help='split = one package per skin (single PKGBUILD), '
                         'single = one package with all skins, '
                         'parallel = separate subdirectory per skin '
                         '(default: split)')
    ap.add_argument('--skins-dir', default=DEFAULT_SKINS_DIR,
                    help=f'install directory for skins (default: {DEFAULT_SKINS_DIR})')
    ap.add_argument('--depends', nargs='*', default=['aimp'],
                    help='package dependencies (default: aimp)')
    ap.add_argument('--maintainer', default=DEFAULT_MAINTAINER,
                    help='maintainer line, "Name <email>" (empty string to omit)')
    ap.add_argument('--url', default=DEFAULT_URL,
                    help=f'url= field of the package (default: {DEFAULT_URL})')
    ap.add_argument('--pkgver', default=f"{date.today():%Y%m%d}",
                    help='pkgver (default: today as YYYYMMDD)')
    ap.add_argument('--pkgrel', default='1', help='pkgrel (default: 1)')
    ap.add_argument('--srcinfo', nargs='?', const='.SRCINFO', default=None,
                    metavar='FILE',
                    help='also write .SRCINFO (default file name: .SRCINFO)')
    ap.add_argument('--publish', action='store_true',
                    help='(parallel mode) git init / commit / push each package '
                         'to the AUR')
    ap.add_argument('--refresh', action='store_true',
                    help='(parallel mode) git pull --ff-only and run updpkgsums '
                         'in each package directory (use --write to save to CSV)')
    ap.add_argument('--write', action='store_true',
                    help='(with --refresh) save updated sha256 checksums from '
                         'updpkgsums back to the CSV file')
    ap.add_argument('--clone', action='store_true',
                    help='clone each package from the AUR via HTTPS '
                         '(https://aur.archlinux.org/<pkg>.git)')
    ap.add_argument('--init', action='store_true',
                    help='(parallel mode) git clone each package from the AUR '
                         'via SSH before generating')
    ap.add_argument('--check', action='store_true',
                    help='check publication status of all parallel packages '
                         '(lists published and not yet published)')
    ap.add_argument('--cmd', metavar='COMMAND',
                    help='run a shell command in every subdirectory of '
                         f'{PKGBASE}/ (requires the directory to exist)')
    ap.add_argument('-j', '--jobs', nargs='?', const=os.cpu_count() or 4,
                    type=int, default=1, metavar='N',
                    help='number of concurrent jobs/threads for bulk operations '
                         '(default: 1; if -j given without N: CPU count)')
    args = ap.parse_args()

    if args.jobs < 1:
        ap.error("-j / --jobs must be at least 1")

    if args.write and not args.refresh:
        sys.exit("--write requires --refresh")

    # --- handle --cmd early: no CSV needed ---
    if args.cmd is not None:
        if not os.path.isdir(PKGBASE):
            sys.exit(f"Directory {PKGBASE}/ does not exist. "
                     f"Generate with --mode parallel first.")
        run_cmd_each(PKGBASE, args.cmd, jobs=args.jobs)
        return

    # --- handle --check early: works with or without CSV ---
    if args.check:
        if not os.path.isdir(PKGBASE):
            sys.exit(f"Directory {PKGBASE}/ does not exist. "
                     f"Generate with --mode parallel first.")
        skins = None
        if args.csv and os.path.isfile(args.csv):
            rows = read_rows(args.csv)
            if rows:
                skins = prepare(rows)
        check_skins_status(PKGBASE, skins=skins, jobs=args.jobs)
        return

    if not args.csv:
        if os.path.isfile('aimp_skins.csv'):
            args.csv = 'aimp_skins.csv'
        else:
            ap.error("the following arguments are required: csv")

    # If parallel-specific operations are requested, default mode to parallel
    if args.publish or args.refresh or args.init or args.clone:
        if args.mode not in ('parallel', 'split'):
            sys.exit("--clone, --init, --refresh and --publish require --mode parallel")
        args.mode = 'parallel'

    rows = read_rows(args.csv)
    if not rows:
        sys.exit("No usable rows found in the CSV")
    skins = prepare(rows)

    if args.mode == 'split' and len(skins) > SPLIT_WARN_THRESHOLD:
        print(f"Warning: split mode with {len(skins)} sub-packages. makepkg needs "
              f"~0.4 s per sub-package on every run (minutes in total). "
              f"Consider --mode single or --mode parallel and use --srcinfo.",
              file=sys.stderr)

    if args.mode == 'parallel':
        # Each skin gets its own subdirectory: aimp-skins/<pkg>/PKGBUILD + .SRCINFO
        base_dir = PKGBASE

        if args.clone:
            print(f"Cloning from AUR via HTTPS ({args.jobs} jobs) ...")
            aur_clone(base_dir, skins, jobs=args.jobs)

        if args.init:
            print(f"Cloning from AUR via SSH ({args.jobs} jobs) ...")
            aur_init(base_dir, skins, jobs=args.jobs)

        if args.refresh:
            print(f"Refreshing from AUR and running updpkgsums ({args.jobs} jobs) ...")
            aur_refresh(base_dir, skins, args=args, jobs=args.jobs)

        skip_generate = args.refresh or (args.clone and not args.publish and '--mode' not in sys.argv)
        if not skip_generate:
            os.makedirs(base_dir, exist_ok=True)

            def _generate_skin(skin):
                skin_dir = os.path.join(base_dir, skin['pkg'])
                os.makedirs(skin_dir, exist_ok=True)
                pkgbuild_path = os.path.join(skin_dir, 'PKGBUILD')
                write_lines(pkgbuild_path, build_parallel_pkgbuild(args, skin))
                srcinfo_path = os.path.join(skin_dir, '.SRCINFO')
                write_lines(srcinfo_path, build_parallel_srcinfo(args, skin))

            if args.jobs > 1:
                with ThreadPoolExecutor(max_workers=args.jobs) as pool:
                    list(pool.map(_generate_skin, skins))
            else:
                for skin in skins:
                    _generate_skin(skin)

            print(f"Done: {base_dir}/ ({len(skins)} skins, mode parallel — "
                  f"each has PKGBUILD + .SRCINFO)")

        if args.publish:
            print(f"Publishing to AUR ({args.jobs} jobs) ...")
            aur_publish(base_dir, skins, jobs=args.jobs)
    else:
        lines = (build_split(args, skins) if args.mode == 'split'
                 else build_single(args, skins))
        write_lines(args.output, lines)
        print(f"Done: {args.output} ({len(skins)} skins, mode {args.mode})")

        if args.srcinfo:
            write_lines(args.srcinfo, build_srcinfo(args, skins))
            print(f"Done: {args.srcinfo}")


if __name__ == '__main__':
    main()
