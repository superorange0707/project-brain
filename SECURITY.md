# Security Policy

## Supported versions

Project Brain is currently alpha software. Security fixes are applied to the most
recent minor release.

| Version | Supported |
|---|---|
| 0.3.x | Yes |
| 0.2.x | No |
| 0.1.x | No |
| Older | No |

## Reporting a vulnerability

Please use **GitHub → Security → Report a vulnerability** to open a private
security advisory. Do not include credentials, proprietary source, or an exploitable
proof of concept in a public issue.

Include the affected version, operating system, impact, reproduction steps, and a
minimal sanitized example. You should receive an acknowledgement within seven
days.

## Threat model

Project Brain is designed as a local, read-only retrieval tool:

- It never asks for API keys or account credentials.
- Its only normal network operation is `git fetch origin`, delegated to the
  user's installed Git and credential helper.
- It does not execute code from target repositories.
- It does not edit, clean, reset, checkout, build, or commit target repositories.
- Subprocesses use argument arrays rather than a shell.
- Direct file requests are constrained to configured repository roots.
- The GUI listens on IPv4 loopback only and every API call requires a random,
  per-process bearer token.
- The GUI rejects non-local Host headers, caps JSON requests at 4 MiB, disables
  caching/framing/referrers, and restricts page connections to itself.
- GUI artifact reads use a generated session allowlist; path traversal and
  `session.json` access through that endpoint are rejected.
- Generated context, local configs, knowledge, state, `.env`, keys, and certificate
  formats are ignored by the default `.gitignore`.

Project Brain does not inspect or persist Git credentials and does not record
remote URLs. Fetch errors are sanitized to an exit status. It invokes locally
installed `git`, `rg`, the optional structural backend, and operating-system
clipboard commands. It trusts executables resolved from the user's `PATH`.

Remote commits are exported with `git archive` into the ignored Brain state
directory. Archive extraction rejects absolute paths, parent traversal, links,
and device entries. Target working trees are never checked out or modified.

The `brain ui` URL contains its ephemeral token. Treat it as local session data,
do not paste it into chat, and stop the process when finished. The token becomes
invalid when the process exits. Project Brain never binds this UI to a LAN or
public interface.

## Source disclosure risk

The largest practical risk is not a Project Brain credential: it is source or a
secret already present in a target repository being included in generated context.
Project Brain cannot know whether the destination chat is approved by your employer
or repository owner.

Before pasting or uploading context:

1. inspect `.runs/<ticket>/context-*.md`;
2. remove credentials and unnecessary personal/proprietary data;
3. confirm the selected AI service is approved for that code;
4. prefer organization-managed AI accounts and retention controls for private code.

## Release security

The release workflow does not use a stored PyPI token. PyPI publishing is gated by
a protected `pypi` environment and uses GitHub OIDC Trusted Publishing, which mints
a short-lived credential for one workflow run. GitHub release creation uses the
ephemeral repository `GITHUB_TOKEN` with scoped permissions.

Maintainers should require manual approval on the `pypi` environment and carefully
review changes to `.github/workflows/release.yml`.
