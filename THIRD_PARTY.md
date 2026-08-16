# Third-party software

Project Brain standalone archives include
[`codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp) v0.10.5,
an MIT-licensed local structural code intelligence engine.

Release builds download the official platform archive and require its pinned
SHA-256 before packaging it. The upstream `LICENSE` and
`THIRD_PARTY_NOTICES.md` files are included in every standalone archive. Project
Brain invokes only the documented JSON CLI and stores its cache below the local
Brain `state/` directory; it does not run the upstream installer or modify AI
client configuration.

Project Brain remains usable without this executable through its deterministic
exact-search and lexical-analysis fallback.
