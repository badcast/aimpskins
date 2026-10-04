#!/usr/bin/env python3
"""
autoferch.py - Automatically fetch latest AIMP for Linux (x86-64 PKG)
version and SHA256 from https://aimp.ru/?do=download&os=linux and update
PKGBUILD, changelog, and .SRCINFO.
"""

import argparse
import hashlib
import http.cookiejar
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

DOWNLOAD_PAGE_URL = "https://aimp.ru/?do=download&os=linux"
BASE_URL = "https://aimp.ru"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"


def fetch_url(url: str, cookies: Optional[http.cookiejar.CookieJar] = None) -> str:
    """Fetch text content of a URL with custom user-agent and optional cookiejar."""
    handlers = []
    if cookies is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookies))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", USER_AGENT)]
    with opener.open(url, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def resolve_redirect(url: str) -> str:
    """Resolve redirect for a URL and return the final target URL."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.geturl()


def parse_download_page(html: str) -> Dict[str, str]:
    """Parse download page and extract version, PKG sha256, download URL and changelog link."""
    result: Dict[str, str] = {}

    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        pkg_card = None
        pkg_li = None

        for card in soup.find_all("div", class_="card"):
            for li in card.find_all("li", class_="link"):
                if "64-bit (PKG)" in li.get_text():
                    pkg_card = card
                    pkg_li = li
                    break
            if pkg_card:
                break

        if not pkg_card or not pkg_li:
            raise ValueError("Could not find '64-bit (PKG)' section on download page.")

        # Extract title / version from card header
        h1 = pkg_card.find("h1")
        if not h1:
            raise ValueError("Could not find version header (h1) in PKG card.")
        card_title = h1.get_text().strip()

        # Extract SHA256 from pkg_li
        li_text = pkg_li.get_text()
        sha_match = re.search(r"SHA256:\s*([a-fA-F0-9]{64})", li_text)
        if not sha_match:
            raise ValueError(f"Could not find SHA256 in PKG section:\n{li_text}")
        pkg_sha256 = sha_match.group(1).lower()

        # Extract download link from pkg_li
        a_tag = pkg_li.find("a", href=True)
        if not a_tag:
            raise ValueError("Could not find download link <a> in PKG section.")
        download_url = urllib.parse.urljoin(BASE_URL, a_tag["href"])

        # Extract changelog link from card actions if available
        changelog_url = None
        ch_a = pkg_card.find("a", href=re.compile(r"do=changelog"))
        if ch_a and ch_a.get("href"):
            changelog_url = urllib.parse.urljoin(BASE_URL, ch_a["href"])

    else:
        # Fallback regex parser
        pkg_pos = html.find("64-bit (PKG)")
        if pkg_pos == -1:
            raise ValueError("Could not find '64-bit (PKG)' on download page.")

        # Search backwards for nearest <h1>
        before = html[:pkg_pos]
        h1_matches = list(re.finditer(r"<h1[^>]*>(.*?)</h1>", before, re.S | re.I))
        if not h1_matches:
            raise ValueError("Could not find <h1> preceding 64-bit (PKG).")
        card_title = re.sub(r"<[^>]+>", "", h1_matches[-1].group(1)).strip()

        # Search for li block containing 64-bit (PKG)
        li_match = re.search(r"(<li[^>]*class=[\"']link[\"'][^>]*>.*?64-bit\s*\(PKG\).*?</li>)", html, re.S)
        if not li_match:
            raise ValueError("Could not extract <li> block for 64-bit (PKG).")
        li_html = li_match.group(1)

        sha_match = re.search(r"SHA256:\s*([a-fA-F0-9]{64})", li_html)
        if not sha_match:
            raise ValueError("Could not find SHA256 in PKG block.")
        pkg_sha256 = sha_match.group(1).lower()

        dl_match = re.search(r"href=[\"']([^\"']+)[\"']", li_html)
        if not dl_match:
            raise ValueError("Could not find download link in PKG block.")
        download_url = urllib.parse.urljoin(BASE_URL, dl_match.group(1))

        ch_match = re.search(r"href=[\"']([^\"']*do=changelog[^\"']*)[\"']", html[pkg_pos:])
        changelog_url = urllib.parse.urljoin(BASE_URL, ch_match.group(1)) if ch_match else None

    # Parse version components: e.g. "AIMP v6.00.3088 Beta 7"
    # Matches: major="6.00", build="3088", stage="Beta 7"
    vm = re.search(r"AIMP\s+v?(\d+\.\d+)\.(\d+)(?:\s+(.*?))?$", card_title, re.I)
    if not vm:
        raise ValueError(f"Unable to parse version numbers from header: '{card_title}'")

    ver_major = vm.group(1)
    ver_build = vm.group(2)
    stage = vm.group(3).strip() if vm.group(3) else ""

    result = {
        "card_title": card_title,
        "ver_major": ver_major,
        "ver_build": ver_build,
        "stage": stage,
        "full_ver": f"{ver_major}.{ver_build}",
        "sha256": pkg_sha256,
        "download_url": download_url,
        "changelog_url": changelog_url or f"{BASE_URL}/?do=changelog&os=desktop&ver={ver_major.replace('.', '')}",
    }
    return result


def fetch_changelog_entry(changelog_url: str, ver_build: str) -> Optional[str]:
    """Fetch the release notes in English for the given build number."""
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.addheaders = [("User-Agent", USER_AGENT)]
        # Switch to English
        opener.open(f"{BASE_URL}/?do=lang&lng=en", timeout=15)
        # Fetch changelog page
        resp = opener.open(changelog_url, timeout=20)
        html = resp.read().decode("utf-8", errors="replace")

        if HAS_BS4:
            soup = BeautifulSoup(html, "html.parser")
            for card in soup.find_all("div", class_="card_content"):
                h1 = card.find("h1")
                if h1 and ver_build in h1.get_text():
                    header = h1.get_text().strip()
                    lines = [header, "=" * len(header)]
                    for p in card.find_all("p"):
                        cls = p.get("class", [])
                        prefix = "+ " if "chlog_new" in cls else ("- " if "chlog_fix" in cls else "* ")
                        lines.append(f"{prefix}{p.get_text().strip()}")
                    return "\n".join(lines)
        else:
            # Simple text parsing fallback
            if ver_build in html:
                m = re.search(r"<h1>(AIMP v[^<]*" + re.escape(ver_build) + r"[^<]*)</h1>", html)
                if m:
                    header = m.group(1).strip()
                    lines = [header, "=" * len(header)]
                    # Extract items up to next card or end
                    p_items = re.findall(r"<p\s+class=[\"']([^\"']+)[\"']>([^<]+)</p>", html)
                    for cls, txt in p_items:
                        prefix = "+ " if "chlog_new" in cls else ("- " if "chlog_fix" in cls else "* ")
                        lines.append(f"{prefix}{txt.strip()}")
                    return "\n".join(lines)
    except Exception as e:
        print(f"[!] Warning: Failed to fetch changelog entry: {e}", file=sys.stderr)
    return None


def calculate_sha256(file_path: Path) -> str:
    """Calculate SHA256 of a local file."""
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def read_pkgbuild_version(pkgbuild_path: Path) -> Optional[Tuple[str, str]]:
    """Read _pkver from PKGBUILD. Returns (ver_major, ver_build)."""
    if not pkgbuild_path.is_file():
        return None
    content = pkgbuild_path.read_text(encoding="utf-8")
    m = re.search(r"_pkver=\(\s*([0-9\.]+)\s+([0-9]+)\s*\)", content)
    if m:
        return m.group(1), m.group(2)
    return None


def construct_source_url(target_url: str, ver_major: str, ver_build: str) -> str:
    """Construct PKGBUILD source URL with ${_pkver[0]} and ${_pkver[1]} variables."""
    # Example: https://aimp.ru/files/desktop/builds/aimp-6.00.3088beta7-1-x86_64.pkg.tar.zst
    # Replace '6.00.3088' with '${_pkver[0]}.${_pkver[1]}'
    ver_dot = f"{ver_major}.{ver_build}"
    ver_hyphen = f"{ver_major}-{ver_build}"
    if ver_dot in target_url:
        return target_url.replace(ver_dot, "${_pkver[0]}.${_pkver[1]}")
    elif ver_hyphen in target_url:
        return target_url.replace(ver_hyphen, "${_pkver[0]}-${_pkver[1]}")
    return target_url


def update_pkgbuild(
    pkgbuild_path: Path,
    ver_major: str,
    ver_build: str,
    source_url: str,
    pkg_sha256: str,
    changelog_sha256: str,
) -> str:
    """Generate updated content for PKGBUILD."""
    content = pkgbuild_path.read_text(encoding="utf-8")

    # Update _pkver
    content = re.sub(
        r"_pkver=\([^)]*\)",
        f"_pkver=({ver_major} {ver_build})",
        content,
    )

    # Reset pkgrel to 1
    content = re.sub(r"^pkgrel=\d+", "pkgrel=1", content, flags=re.M)

    # Update source line
    content = re.sub(
        r'(source=\(\s*")[^"]+(")',
        lambda m: f'{m.group(1)}{source_url}{m.group(2)}',
        content,
    )

    # Update sha256sums
    content = re.sub(
        r"sha256sums=\(\s*'([0-9a-fA-F]{64})'\s*\n\s*'([0-9a-fA-F]{64})'\s*\)",
        lambda m: f"sha256sums=('{pkg_sha256}'\n            '{changelog_sha256}')",
        content,
    )

    return content


def update_srcinfo(repo_dir: Path) -> bool:
    """Run makepkg --printsrcinfo > .SRCINFO."""
    try:
        proc = subprocess.run(
            ["makepkg", "--printsrcinfo"],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        (repo_dir / ".SRCINFO").write_text(proc.stdout, encoding="utf-8")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[!] makepkg --printsrcinfo failed:\n{e.stderr}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[!] makepkg binary not found. Skipping .SRCINFO generation.", file=sys.stderr)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch latest AIMP for Linux version and SHA256 and update PKGBUILD/SRCINFO."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Repository directory containing PKGBUILD (default: script directory)",
    )
    parser.add_argument(
        "--url",
        type=str,
        default=DOWNLOAD_PAGE_URL,
        help="AIMP Linux download page URL",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Force updating files even if already on the latest version",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Simulate the update without modifying any files",
    )
    parser.add_argument(
        "--check",
        "-c",
        action="store_true",
        help="Check latest version and sha256 and exit without modifying files",
    )
    parser.add_argument(
        "--no-changelog",
        action="store_true",
        help="Do not fetch or prepend new release notes to changelog",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Create a git commit after applying updates",
    )

    args = parser.parse_args()
    repo_dir: Path = args.dir
    pkgbuild_path = repo_dir / "PKGBUILD"
    changelog_path = repo_dir / "changelog"

    if not pkgbuild_path.is_file():
        print(f"[-] Error: PKGBUILD not found in {repo_dir}", file=sys.stderr)
        return 1

    print(f"[*] Fetching download page: {args.url}")
    try:
        html = fetch_url(args.url)
    except Exception as e:
        print(f"[-] Error fetching download page: {e}", file=sys.stderr)
        return 1

    print("[*] Parsing latest AIMP version and package checksum...")
    try:
        info = parse_download_page(html)
    except Exception as e:
        print(f"[-] Parsing error: {e}", file=sys.stderr)
        return 1

    print(f"    Release:      {info['card_title']}")
    print(f"    Version:      {info['full_ver']} (major={info['ver_major']}, build={info['ver_build']})")
    print(f"    SHA256 (PKG): {info['sha256']}")
    print(f"    Download URL: {info['download_url']}")

    print("[*] Resolving build redirect URL...")
    try:
        target_url = resolve_redirect(info["download_url"])
        print(f"    Direct URL:   {target_url}")
    except Exception as e:
        print(f"[!] Warning: Could not resolve redirect ({e}), using download URL directly.", file=sys.stderr)
        target_url = info["download_url"]

    # Check current PKGBUILD version
    current_ver = read_pkgbuild_version(pkgbuild_path)
    if current_ver:
        cur_major, cur_build = current_ver
        print(f"[*] Current PKGBUILD version: {cur_major}.{cur_build}")
        if (cur_major, cur_build) == (info["ver_major"], info["ver_build"]) and not args.force:
            print("[+] Package is already up to date!")
            if not args.check:
                return 0
    else:
        print("[!] Warning: Could not read existing _pkver from PKGBUILD.", file=sys.stderr)

    if args.check:
        return 0

    # Source URL for PKGBUILD
    source_url = construct_source_url(target_url, info["ver_major"], info["ver_build"])
    print(f"[*] Parameterized source URL: {source_url}")

    # Changelog handling
    updated_changelog_content = None
    if changelog_path.is_file():
        current_changelog = changelog_path.read_text(encoding="utf-8")
        if not args.no_changelog:
            if info["ver_build"] in current_changelog.split("\n\n")[0]:
                print(f"[*] Changelog already contains entry for build {info['ver_build']}.")
            else:
                print(f"[*] Fetching changelog for build {info['ver_build']}...")
                entry = fetch_changelog_entry(info["changelog_url"], info["ver_build"])
                if entry:
                    print(f"[+] Retrieved changelog entry ({len(entry.splitlines())} lines).")
                    updated_changelog_content = entry + "\n\n" + current_changelog
                else:
                    print("[!] No changelog entry found online. Preserving existing changelog.")
        if updated_changelog_content:
            changelog_sha256 = hashlib.sha256(updated_changelog_content.encode("utf-8")).hexdigest()
        else:
            changelog_sha256 = calculate_sha256(changelog_path)
    else:
        changelog_sha256 = ""

    print(f"[*] Changelog SHA256: {changelog_sha256}")

    # Generate updated PKGBUILD
    new_pkgbuild = update_pkgbuild(
        pkgbuild_path=pkgbuild_path,
        ver_major=info["ver_major"],
        ver_build=info["ver_build"],
        source_url=source_url,
        pkg_sha256=info["sha256"],
        changelog_sha256=changelog_sha256,
    )

    if args.dry_run:
        print("\n--- [DRY RUN] Updated PKGBUILD preview ---")
        print(new_pkgbuild)
        if updated_changelog_content:
            print("\n--- [DRY RUN] Changelog prepended entry ---")
            print(updated_changelog_content.split("\n\n")[0])
        print("\n--- [DRY RUN] Completed without writing files ---")
        return 0

    # Write changelog
    if updated_changelog_content:
        changelog_path.write_text(updated_changelog_content, encoding="utf-8")
        print("[+] changelog updated.")

    # Write PKGBUILD
    pkgbuild_path.write_text(new_pkgbuild, encoding="utf-8")
    print("[+] PKGBUILD updated.")

    # Update .SRCINFO
    print("[*] Generating .SRCINFO with makepkg...")
    if update_srcinfo(repo_dir):
        print("[+] .SRCINFO updated successfully.")
    else:
        print("[!] Warning: Failed to update .SRCINFO automatically.", file=sys.stderr)

    print("\n[✔] Successfully updated AIMP package to " + info["card_title"])

    if args.commit:
        commit_msg = f"AIMP {info['ver_major']} {info['ver_build']} {info['stage'].lower()}".strip()
        print(f"[*] Creating git commit: {commit_msg}")
        try:
            subprocess.run(
                ["git", "add", "PKGBUILD", "changelog", ".SRCINFO"],
                cwd=repo_dir,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=repo_dir,
                check=True,
            )
            print("[+] Changes committed to git.")
        except subprocess.CalledProcessError as e:
            print(f"[-] Git commit failed: {e}", file=sys.stderr)
            return 1
    else:
        stage_str = f" {info['stage'].lower()}" if info['stage'] else ""
        print(f"[*] To commit changes, run:\n    git commit -am 'AIMP {info['ver_major']} {info['ver_build']}{stage_str}'")

    return 0


if __name__ == "__main__":
    sys.exit(main())
