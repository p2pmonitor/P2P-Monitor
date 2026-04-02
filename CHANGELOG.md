# Changelog

## v1.1.5
- Fixed root cause of account showing Offline during a break — backwards scan was treating 'Break over -> Startup' (a skip notification) as a completed break; fixed in backwards scan, live poll loop, and timestamp pair math to only match 'Break over N' (with a number)
- Fixed break time overwrite — completed break total from session files was overwriting the current in-progress elapsed time; now sets total_break_secs from completed breaks first, then adds current break elapsed on top
- Fixed BREAK START forward scan finding first instead of last — break_expected_end was calculated from an old break; now always uses the last BREAK START in the file
- Status tab auto-refreshes every 30 seconds so uptime and break time tick live
- Screenshots now wait up to 60 seconds for Discord gateway before bot delivery; webhook delivery unaffected
- Auto-updater now uses update_manifest.txt from the release zip to determine which files to apply — no more hardcoded file list; add new files to the manifest and they ship automatically
- Update apply is now staged — files are extracted to a temp dir on the same filesystem, manifest is verified, then files are copied to install dir; staging dir always cleaned up on success or failure
- Release selection now sorts by parsed semver instead of published_at date
- Zip asset selection now prefers P2P-Monitor-*.zip by name prefix before falling back to any .zip
- _local_ver() now reads the VERSION constant directly instead of scraping file text

## v1.1.4
- Fixed account showing Offline during a break when the log has multiple break sessions — startup catchup was finding the first BREAK START instead of the last
- Fixed screenshots failing before the Discord gateway connects — bot screenshots now wait up to 60s for the gateway; webhook screenshots unaffected

## v1.1.3
- Fixed silent startup update check passing boolean False as asset URL when user accepted the update prompt — caused "unknown url type: 'False'" download error

## v1.1.2
- Fixed auto-updater incorrectly extracting all files flat into root install directory instead of preserving py/ and ui/ subfolder structure

## v1.1.1
- Fixed break time calculation — now uses BREAK START / Break over timestamps instead of the logged ms value; DreamBot logs -100ms for manually skipped breaks which was corrupting the total

## v1.1.0
- Auto-updater now uses GitHub Releases — downloads full release zip, applies only changed files, cleans up after itself
- Added beta opt-in checkbox in Settings — manual update checks include pre-release versions when enabled; silent startup check always uses stable releases only
- Fixed break time not accumulating correctly after monitor restart
- Fixed paint overlay incorrectly hiding on startup screenshots when hide option is unchecked — caused by window not being fully rendered before button state was read; now waits for window to settle and verifies state after each click
- Added CHANGELOG.md, LICENSE (GPL v3), .gitignore

## v1.0.0
- Initial public release
