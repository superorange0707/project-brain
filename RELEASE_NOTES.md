# Project Brain v1.0.37 — Starts Reliably Where Login Items Are Blocked

Brain now handles Macs that will not run its login service, and upgrades show
progress while the index is republished.

## What changed

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

On Windows, `brain upgrade` works the same way; the installer with
`-Version 1.0.37` also works. Workspaces, model packs, ticket history and the
published index are kept; no refresh or Agent Kit change is needed.

---

Earlier releases are described in the [Changelog](CHANGELOG.md) and on the
[Releases page](https://github.com/superorange0707/project-brain/releases).
