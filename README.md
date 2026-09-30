# aimpskins

Generate an Arch Linux / AUR `PKGBUILD` (and `.SRCINFO`) for [AIMP](https://www.aimp.ru) skins from a CSV list.

Give it a CSV with the skin catalog from aimp.ru and it produces either:

- **split mode**: one package per skin (`aimp-skin-<name>`), or
- **single mode**: one package (`aimp-skins`) that installs every skin, or
- **parallel mode**: each skin gets its own subdirectory `aimp-skins/aimp-skin-<slug>/` with a standalone `PKGBUILD` and `.SRCINFO`; all packages belong to the `AIMP_Skins` group.

It also writes a valid `.SRCINFO` directly, because `makepkg --printsrcinfo` is very slow on PKGBUILDs with hundreds of split packages (see [Performance](#performance)).

## Requirements

- Python 3.6+
- `csv2pkgbuild.py` uses the standard library only
- `refresh_list.py` needs `requests` and `beautifulsoup4` (`pip install requests beautifulsoup4`)
- To build the generated package: `makepkg` and `libarchive` (`bsdtar`)

## 1. Get the CSV

Run `refresh_list.py`. It downloads the whole skins catalog from aimp.ru (`https://aimp.ru/?do=catalog&os=desktop&id=0&pagesize=99999`, all skins on a single page), parses the skin cards and writes `aimp_skins.csv` into the current directory.

```bash
pip install requests beautifulsoup4
python3 refresh_list.py
```

On success it prints the number of rows and how many of them have an empty version:

```
Done: <N> rows -> aimp_skins.csv (empty version: 0)
```

What the script does for every card on the page:

- **name / version**: the title is split into a name and a version. A `v1.2`-style token anywhere in the title is the version (`Mayak-203 (205) v3.27` becomes `Mayak-203 (205)` / `v3.27`); otherwise a trailing dotted number is used (`A4 3.2` becomes `A4` / `3.2`).
- **version fallback**: many skins have no version in the title (`Technicss`, `Kenwood KX-4520`, ...). For them the `version` column contains the publication date from the card (`YYYY-MM-DD`), so it is never empty.
- **author**: taken from the card header.
- **link**: the absolute `catalog.download` URL.
- **sha256**: always `SKIP`. Fill real checksums later with `csv2pkgbuild.py --refresh --write` (see [Parallel Mode & AUR Workflow](#parallel-mode--aur-workflow)).
- The file is written as UTF-8 with BOM, all fields quoted, `\r\n` line endings.

> **Warning:** `refresh_list.py` overwrites `aimp_skins.csv` and resets every `sha256` to `SKIP`. If you already have checksums in the CSV, keep a backup (`cp aimp_skins.csv aimp_skins.csv.bak`) or run `--refresh --write` again afterwards.

The catalog URL and the output file name are the `URL` and `OUT` constants at the top of the script.

### CSV format

```csv
number,name,version,author,link,sha256
1,Soot,v4.0.1,gr-e,https://www.aimp.ru/?do=catalog.download&id=923,675bead4376bf1cbc5a35d09c14100a7128295d25b82594016c5f6041f0d71ea
2,A4,3.2,ELECTRON!CK,https://www.aimp.ru/?do=catalog.download&id=832,c3a2e65609ff6757809d6842130f7ea403acf7ae02a64e749e37e8b304870904
```

| Column    | Required | Notes                                                        |
|-----------|----------|--------------------------------------------------------------|
| `name`    | yes      | Used for the package name and the install directory          |
| `link`    | yes      | Must contain `id=<number>`; rows without it are skipped      |
| `version` | no       | Only shown in the package description. `refresh_list.py` uses the publication date if the title has no version |
| `author`  | no       | Only shown in the package description                        |
| `sha256`  | no       | SHA-256 checksum of the archive; used in `sha256sums`. If empty, falls back to `SKIP` |
| `number`  | no       | Ignored                                                      |

The file may be UTF-8 with or without BOM.

## 2. Generate

```bash
# recommended for large lists: one package + instant .SRCINFO
python3 csv2pkgbuild.py aimp_skins.csv -o PKGBUILD --mode single --srcinfo

# one package per skin (single PKGBUILD with split packages)
python3 csv2pkgbuild.py aimp_skins.csv -o PKGBUILD --mode split --srcinfo

# separate directory per skin (each gets its own PKGBUILD + .SRCINFO)
python3 csv2pkgbuild.py aimp_skins.csv --mode parallel

# generate + push every package to the AUR
python3 csv2pkgbuild.py aimp_skins.csv --mode parallel --publish

# clone existing AUR repos first, then regenerate and push
python3 csv2pkgbuild.py aimp_skins.csv --mode parallel --init --publish

# pull latest from AUR, run updpkgsums, save new hashes to CSV, and push
python3 csv2pkgbuild.py aimp_skins.csv --mode parallel --refresh --write --publish -j 8

# run any arbitrary command across all skin subdirectories (no CSV needed)
python3 csv2pkgbuild.py --cmd "git add -A ."
python3 csv2pkgbuild.py --cmd "git status --short"

# clone all packages from the AUR via HTTPS (public, no SSH required)
python3 csv2pkgbuild.py --clone -j 8

# check publication status across all parallel packages (no CSV needed)
python3 csv2pkgbuild.py --check -j 8

# speed up bulk operations with concurrent threads (-j / --jobs)
python3 csv2pkgbuild.py --clone -j 8
python3 csv2pkgbuild.py aimp_skins.csv --mode parallel --publish -j 8
python3 csv2pkgbuild.py --cmd "git add -A ." -j

# customised
python3 csv2pkgbuild.py aimp_skins.csv \
    --skins-dir /usr/share/aimp/Skins \
    --depends aimp wine \
    --maintainer "Jane Doe <jane@example.com>" \
    --url https://github.com/you/aimpskins \
    --srcinfo
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `csv` | | Input CSV file (optional when using `--cmd` or `--check`) |
| `-o`, `--output` | `PKGBUILD` | Output PKGBUILD file |
| `--mode {split,single,parallel}` | `split` | `split` = one PKGBUILD with split packages, `single` = one monolithic package, `parallel` = separate subdirectory per skin |
| `-j`, `--jobs [N]` | `1` (CPU count if `-j` without `N`) | Number of concurrent jobs/threads for bulk operations (`--init`, `--refresh`, `--publish`, `--cmd`, `--check`, parallel generation) |
| `--skins-dir PATH` | `/opt/aimp/Skins` | Install directory for skins inside the package |
| `--depends PKG [PKG ...]` | `aimp` | Dependencies of the generated package(s) |
| `--maintainer "Name <email>"` | see script | Maintainer line at the top (empty string omits it) |
| `--url URL` | see script | `url=` field of the package |
| `--pkgver VER` | today (`YYYYMMDD`) | Package version |
| `--pkgrel N` | `1` | Package release |
| `--srcinfo [FILE]` | off (`.SRCINFO` if no name given) | Also write a `.SRCINFO` |
| `--clone` | off | Clone each package from the AUR via HTTPS (`https://aur.archlinux.org/<pkg>.git`). Skips already-cloned directories |
| `--init` | off | (parallel only) `git clone` each package from the AUR via SSH before generating. Skips already-cloned directories |
| `--publish` | off | (parallel only) `git init` / `commit` / `push` each package to the AUR. Sets remote to `ssh://aur@aur.archlinux.org/<pkg>.git` automatically |
| `--refresh` | off | (parallel only) `git pull --ff-only` and run `updpkgsums` in each package directory |
| `--write` | off | (used with `--refresh`) save updated sha256 checksums from `updpkgsums` back into the CSV file |
| `--check` | off | Check publication status of all parallel packages (lists published and unpushed; CSV not needed) |
| `--cmd COMMAND` | none | Run a shell command in every subdirectory of `aimp-skins/` (requires `aimp-skins/` to exist; CSV not needed) |

> The defaults for `--maintainer` and `--url` are constants at the top of `csv2pkgbuild.py`. Change them to your own before publishing.

## How it works

- **Package names**: Cyrillic is transliterated, everything is lowercased and reduced to `[a-z0-9-]`: `САТУРН 202-2С` becomes `aimp-skin-saturn-202-2s`. If two skins collide, the aimp.ru id is appended (`aimp-skin-saturn-336`).
- **Sources**: `source=('<name>.archive::<link>' ...)`. The download links are redirects (`?do=catalog.download&id=N`), so the local file name is set explicitly. `noextract` covers every source.
- **Install**: each archive is extracted with `bsdtar` directly into `<skins-dir>/`. If an archive contains a nested directory, files are moved directly into `<skins-dir>/` (e.g. `/opt/aimp/Skins/Byg.acs3`). Directories get mode 755, files 644.
- **Versions**: all split packages share one `pkgver` (makepkg does not allow per-package versions). The skin's own version is included in its `pkgdesc`.
- **Checksums**: `sha256sums` are populated from the `sha256` column in the CSV. If a checksum is missing, `SKIP` is used.
- **Parallel mode**: each skin is placed in its own directory `aimp-skins/<pkg>/` with an independent `PKGBUILD` and `.SRCINFO`. Every package has `groups=('AIMP_Skins')`, so you can install/remove them as a group.

### Generated layout (excerpt, split mode)

```bash
pkgbase=aimp-skins
pkgname=(
  'aimp-skin-soot'
  'aimp-skin-a4'
)
...
package_aimp-skin-soot() {
  pkgdesc='AIMP skin: Soot v4.0.1 (by gr-e)'
  _install_skin 'aimp-skin-soot-923.archive'
}
```

### Generated layout (parallel mode)

```
aimp-skins/
├── aimp-skin-soot/
│   ├── PKGBUILD
│   └── .SRCINFO
├── aimp-skin-a4/
│   ├── PKGBUILD
│   └── .SRCINFO
└── ...
```

Each `PKGBUILD` is a fully standalone package with `groups=('AIMP_Skins')`.

### Parallel Mode & AUR Workflow

Parallel mode provides built-in tools to manage hundreds of AUR repositories without manual scripting. All network, git, and batch operations support **`-j [N]`** for multi-threaded concurrency:

1. **Clone from AUR via HTTPS (`--clone`)**:
   ```bash
   python3 csv2pkgbuild.py --clone -j 8
   ```
   Clones `https://aur.archlinux.org/<pkg>.git` concurrently into each `aimp-skins/<pkg>/` directory. Uses fast, public HTTPS that requires no SSH keys or configuration and doesn't hit SSH connection rate limits. Skips already-cloned packages. (Alternatively, `--init` clones via SSH `ssh://aur@aur.archlinux.org/<pkg>.git`).

2. **Pull updates from AUR & run `updpkgsums`**:
   ```bash
   python3 csv2pkgbuild.py aimp_skins.csv --mode parallel --refresh --write -j 8
   ```
   Runs `git pull --ff-only` across all skin directories concurrently, recalculates checksums with `updpkgsums`, updates `.SRCINFO`, and (with `--write`) saves changed `sha256` hashes back into `aimp_skins.csv`.

3. **Regenerate & Publish**:
   ```bash
   python3 csv2pkgbuild.py aimp_skins.csv --mode parallel --publish -j 8
   ```
   Updates `PKGBUILD` and `.SRCINFO`, stages them, creates a commit (if there are changes), and pushes to `origin/master`.

4. **Batch operations with `--cmd`**:
   Execute any arbitrary shell command across every subdirectory in `aimp-skins/` (no CSV required):
   ```bash
   # Stage all changes across all directories in parallel
   python3 csv2pkgbuild.py --cmd "git add -A ." -j 8

   # Check git status everywhere
   python3 csv2pkgbuild.py --cmd "git status --short" -j 8

   # Regenerate checksums locally in parallel
   python3 csv2pkgbuild.py --cmd "updpkgsums" -j 4

   # Build/check packages
   python3 csv2pkgbuild.py --cmd "makepkg -od" -j 4
   ```

5. **Audit publication status with `--check`**:
   ```bash
   python3 csv2pkgbuild.py --check -j 8
   ```
   Inspects all packages in parallel and outputs clean categorized lists of published packages (`[✓]`) and not yet published packages (`[✗]` with failure reason), plus the total counts.

## Performance

`makepkg` spends roughly 0.4 s per split sub-package on **every** invocation (`-g`, `-o`, builds, `--printsrcinfo`), so large split PKGBUILDs feel like they hang. Measured with `makepkg --printsrcinfo`:

| PKGBUILD | Time |
|----------|------|
| split, 3 skins | 1.3 s |
| split, 25 skins | 10.6 s |
| split, 50 skins | 20.7 s |
| split, 100 skins | 42.5 s |
| split, 750 skins | several minutes (more than 90 s) |
| single, 750 skins | 2.9 s |

Recommendations:

- Prefer `--mode single` for big lists.
- Use `--mode parallel` if you need separate packages without the split-mode overhead — each skin gets a fast standalone `PKGBUILD`.
- Use `--srcinfo` instead of `makepkg --printsrcinfo`. The generated `.SRCINFO` was checked to be byte-identical to `makepkg` output on test packages.
- If you really need split packages, generate several smaller PKGBUILDs (25-50 skins each).

The script prints a warning when split mode is used with more than 50 skins.

## Build and test

```bash
makepkg -si
```

Before running on the whole catalog, try a CSV with 3-5 skins. To check that a download link works:

```bash
curl -IL --max-time 20 'https://www.aimp.ru/?do=catalog.download&id=923'
```

If downloads are slow or flaky, fetch the archives beforehand and place them next to the PKGBUILD under the names listed in `source=`; `makepkg` will then skip downloading them.

## Publishing to the AUR: things to know

- AUR rejects files larger than 250 KiB. For the full catalog (~750 skins) the generated files are about 205 KiB (split PKGBUILD), 140 KiB (single PKGBUILD), 180 KiB / 120 KiB (`.SRCINFO`). Split mode is close to the limit, and a growing catalog will exceed it. **Parallel mode** avoids this entirely since each package has its own small files.
- Every split package name must be unique in the AUR; one taken name rejects the whole push. In parallel mode each package is pushed independently.
- The catalog is large (about 6 GB at the time of writing). Building any split package downloads the sources of **all** packages in the base, and the single package installs everything. Parallel mode only downloads the single skin's archive. Consider a curated subset, or a small installer package that downloads skins on demand.
- If the `sha256` column is filled in the CSV, real checksums are used. Otherwise `sha256sums=SKIP` is emitted and a date-based `pkgver` means that a skin updated in place on aimp.ru changes without a version bump.
- `license=('custom')` is a placeholder: skins belong to their authors and have their own terms. Set the license(s) appropriately and ship a license file if required.
- Verify that `--skins-dir` matches the directory your AIMP installation actually reads skins from (it differs between the Wine build and the native Linux build).
- Follow the [AUR submission guidelines](https://wiki.archlinux.org/title/AUR_submission_guidelines). Packages that violate them may be deleted.

## Troubleshooting

| Problem | Cause / fix |
|---------|-------------|
| `makepkg` seems to hang | Split mode with many packages. Use `--mode single`, or wait; see [Performance](#performance). |
| `bsdtar: command not found` | Install `libarchive`. |
| Archive fails to extract | The download may be an HTML error page. Check the link with `curl -IL`. |
| Skins land in the wrong place | Adjust `--skins-dir`. |
| Rows skipped on generation | The `link` value lacks `id=<number>`. |
| `refresh_list.py` writes `0 rows` | The site markup changed or the cards are loaded by JavaScript. Open the page, inspect one card and adjust the selectors in the script. |
| Many empty versions in the CSV | Update `refresh_list.py`: the current version falls back to the publication date. |
| Checksums reset to `SKIP` | `refresh_list.py` rewrites the CSV. Run `csv2pkgbuild.py --refresh --write` to recompute them. |

## Disclaimer

This tool only generates packaging scripts that point to files hosted on aimp.ru. The skins are the work of their respective authors.
