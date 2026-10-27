# Project Brain v1.0.38 — Auto Refresh That Keeps Up, Startup That Never Hangs

Idle Auto Refresh now keeps working when some repositories' Git servers are
unreachable, and Brain starts reliably on Macs that block login items.

## What changed

- **Auto Refresh no longer stalls on one bad repository.** Previously, if any
  repository's remote could not be checked (no access, VPN off, a slow server),
  Auto Refresh reported Git freshness as unverified and did nothing, even when
  other repositories had changed or an upgrade needed a republish. Now changes
  elsewhere still refresh, the unreachable repository keeps its newest local
  ref, and the status says how many repositories could not be fetched. Such a
  refresh is shown as the last refresh instead of as a failure.
- **No more hanging `brain ui` on locked-down Macs.** Company MDM can block
  login items, and a login item cannot read a workspace under `~/Documents`,
  `~/Desktop`, `~/Downloads` or iCloud Drive without Full Disk Access. When
  the login service is refused or does not become ready, `brain ui`,
  `brain service install` and `brain upgrade` now remove this workspace's
  login item, start Brain in the background from the terminal and print the
  reason, including the service's own last log line. Later `brain ui` calls
  start immediately; `brain ui --foreground` is no longer needed.
- **Visible upgrade progress.** When `brain upgrade` republishes the index once
  after a version change, it prints the same timestamped phase lines as
  `brain refresh` instead of running silently.

v1.0.37 was tagged but not published; its changes are included here.

## Upgrade from v1.0.36

Run this from your workspace:

```sh
brain upgrade
```

If the macOS login service never started on this Mac (for example, `brain ui`
waited and then reported that the service was not ready), remove it first so
the v1.0.36 upgrader does not wait for it, then upgrade and start Brain:

```sh
brain service uninstall
brain upgrade
brain ui
```

## Upgrade from v1.0.33 or older

These versions do not have `brain upgrade`. On macOS:

```sh
brain ui stop
brew update
brew trust --formula superorange0707/tap/project-brain
brew upgrade superorange0707/tap/project-brain
brain --version
brain ui
```

The version must be `brain 1.0.38`. The index is then republished once,
automatically when **Auto Refresh** is set to *When idle*, otherwise with
**Refresh Brain**. On Windows, rerun the installer with `-Version 1.0.38`.

Workspaces, model packs and ticket history are kept. If `brain status` reports
that the M365 Agent Kit changed, regenerate it with `brain agent-kit m365` and
replace it in Agent Builder.

---

Earlier releases are described in the [Changelog](CHANGELOG.md) and on the
[Releases page](https://github.com/superorange0707/project-brain/releases).
