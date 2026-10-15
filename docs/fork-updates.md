# Fork updates and first migration

Panel updates and the Linux CLI/install scripts in this fork use
`LelandJin/3x-ui` exclusively. Upstream authorship, the Go module path, and
Xray/geodata dependency sources remain unchanged.

## Channels

- `install.sh` without arguments, `x-ui install`, `x-ui update`, and `x-ui`
  Dev update select the fork's rolling `dev-latest` prerelease. `dev` is an alias.
- The panel keeps its saved Dev channel setting. Enable **Dev channel** to
  follow this fork's rolling build; updates compare build commits, even when
  the base semantic version is unchanged. An unknown local commit offers an
  update rather than claiming the build is current.
- An explicit version (for example `install.sh v3.10.2` or `x-ui legacy`)
  must be a release published **in this fork**. `install.sh stable` explicitly
  requests the fork's latest stable release. As of 2026-10-15, this fork has
  no stable release, so stable requests fail with instructions to use Dev.
  There is no fallback to upstream or from stable to Dev.

The existing version-tag workflow also publishes tagged builds with
`prerelease: true`. Pushing a version tag alone therefore does not make
`/releases/latest` available. Stable support requires a separately approved
release/promotion policy; this updater repair keeps the current Dev-only
publication policy unchanged.

## First migration from an older installation

The old deployed panel button and old CLI still download upstream code.
Changing this repository cannot repair those running updaters remotely.
After this PR is reviewed and merged, wait for the fork's normal main build
to publish a complete repaired Dev artifact. This PR itself does not merge,
publish a release, or deploy anything.

On the installation host, an administrator can use the repaired updater:

1. Back up the existing database and local configuration through the normal
   administrative process.
2. Find the full 40-character `commit=` value in the fork's `dev-latest`
   release notes. Use the commit containing this repair (or a later one).
3. Download `https://raw.githubusercontent.com/LelandJin/3x-ui/<FULL_COMMIT>/update.sh`
   to a local file with `curl -fL -o update.sh <URL>`, review it, then run
   `sudo env XUI_UPDATE_TAG=dev-latest XUI_EXPECTED_COMMIT=<FULL_COMMIT> bash ./update.sh`.
   Preserve any existing `XUI_MAIN_FOLDER` / `XUI_SERVICE` overrides in that
   invocation. The updater preserves the existing panel settings and TLS setup.
4. Enable the panel's Dev channel for subsequent panel updates. The installed
   CLI now tracks the same fork; menu/script refresh uses the installed
   `.release-commit`, never the current main branch.

Fresh installations can download and review the repaired `install.sh` at the
same full commit and invoke it with `dev-latest`. Its existing installation
configuration prompts/options still apply. Scripts require `curl`, `jq`,
`tar` and SHA256 tooling; install/update dependency setup installs `jq`.
CLI bootstrap reports a missing `jq` dependency before doing anything else.

## Consistency and failure behavior

The panel and CLI resolve the selected fork release before fetching its updater
or installer at the **full build commit**, and pass that commit to the child.
A moving Dev release causes a retryable error if the child resolves another
commit. Old scripts that predate the fork update protocol are refused.
Standalone downloads from main also re-execute the installer/updater at the
selected full release commit before preparing or replacing the installation.

Install/update then download the archive and its mandatory `.sha256` sidecar
into a private staging directory. They verify the hash, extract and check the
required binaries, and require the archive's `.release-commit` to match the
resolved release commit. This detects mixed assets while `dev-latest` is being
republished. CLI and service files come from that archive; any missing companion
file is fetched at the same full commit **before** stopping the old service.

A missing release, asset, checksum, commit marker or required companion, an
empty/corrupt download, or a mismatched hash/commit aborts before replacing the
existing installation or stopping its service. Older unmarked artifacts are
deliberately unsupported, including historical fork tags. There is no checksum
bypass. SHA256 verifies integrity against the published sidecar; it is not a
separate publisher signature.

Once preparation succeeds, installation still needs a writable filesystem,
sufficient disk space, working systemd/OpenRC and compatible database migrations.
This change does not provide transactional rollback for failures during file
replacement, migrations or service startup. Keep administrative backups.

## Maintenance and validation

`deploy/release-source.sh` is the canonical shell resolver. After editing it,
run `python3 deploy/sync-release-source.py`; it embeds identical code in the
three standalone scripts so no live helper download can drift from them.
Keep its fork constants and Dev tag aligned with `panel.go`.

Linux packaging in `release.yml` must keep writing the full `$GITHUB_SHA` into
`x-ui/.release-commit` and shipping `x-ui.sh`, OpenRC and systemd units before
generating the archive checksum. Do not point companions at main or accept a
rolling release's stale `target_commitish` in place of its build marker.
Every packaged companion must match the release workflow's main push paths
so an isolated service-file fix refreshes Dev. The offline regression contract
checks this relationship and that those inputs also trigger regression CI.

Run `python3 deploy/test/test_fork_updates.py`,
`python3 deploy/sync-release-source.py --check`, and
`bash -n install.sh update.sh x-ui.sh deploy/release-source.sh`.
The offline fixtures exercise resolution and preparation functions only;
mock downloads and aborting service stubs prevent host installation.
Run the panel/config/controller Go tests and frontend checks as in CI.
