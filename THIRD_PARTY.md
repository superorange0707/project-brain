# Third-party software

Project Brain standalone archives include
[`codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp) v0.10.5,
an MIT-licensed local structural code intelligence engine.

They also include [`Zoekt`](https://github.com/sourcegraph/zoekt)
v0.0.0-20251202141441-886b229dcd5e, an Apache-2.0-licensed local trigram code
search engine. The release workflow compiles `zoekt` and `zoekt-index` from that
exact Go module revision, includes the upstream license and revision file in the
archive, and publishes a SHA-256 manifest for the final archive.

Release builds download the official platform archive and require its pinned
SHA-256 before packaging it. The upstream `LICENSE` and
`THIRD_PARTY_NOTICES.md` files are included in every standalone archive. Project
Brain invokes only the documented JSON CLI and stores its cache below the local
Brain `state/` directory; it does not run the upstream installer or modify AI
client configuration.

Project Brain remains usable without this executable through its deterministic
exact-search and lexical-analysis fallback.
