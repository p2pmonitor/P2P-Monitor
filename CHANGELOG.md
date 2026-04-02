# Changelog

## v1.1.3
- Fixed silent startup update check passing boolean False as asset URL when user accepted the update prompt — caused "unknown url type: 'False'" download error

## v1.1.2
- Fixed auto-updater incorrectly extracting all files flat into the root install directory instead of preserving py/ and ui/ subfolder structure

## v1.1.1
- Fixed break time calculation — now uses timestamp math (BREAK START → Break over) instead of the logged ms value; DreamBot logs -100 for manually skipped breaks which was corrupting the total

## v1.1.0
- Auto-updater now uses GitHub Releases — downloads full release zip, applies only changed files, cleans up after itself
- Added beta opt-in checkbox in Settings — manual update checks include pre-release versions when enabled; silent startup check always uses stable releases only
- Fixed break time not accumulating correctly after monitor restart
- Fixed paint overlay incorrectly hiding on startup screenshots when hide option is unchecked — caused by window not being fully rendered before button state was read; now waits for window to settle and verifies state after each click
- Added LICENSE (GPL v3) and .gitignore

## v1.0.0
- Initial public release
