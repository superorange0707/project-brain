# Contributing to Project Brain

Thanks for helping ordinary chat AIs understand real codebases more reliably.

## Before opening a change

- Search existing issues.
- Keep the tool local, read-only, deterministic, and model-independent.
- Prefer the Python standard library and existing code over new dependencies.
- Do not include company code, credentials, local absolute paths, or generated
  `.runs` context in an issue, fixture, commit, or screenshot.
- For security problems, follow [SECURITY.md](SECURITY.md) instead of filing a public
  issue.

## Development setup

```bash
git clone https://github.com/superorange0707/project-brain.git
cd project-brain
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

On Windows, activate with `.venv\Scripts\activate`.

## Pull requests

Maintainers follow the [release procedure](docs/RELEASING.md) for builds,
artifact verification and publication.

Hosted CI is manual: run the CI workflow from the Actions tab when a change
needs hosted Linux or native Windows coverage. Pull requests and pushes start no
hosted runs; run the checks below locally and record the results in the pull
request. Tagged releases run the full OS/Python matrix and native packaging.

Keep pull requests focused. Include:

- the retrieval or workflow problem being solved;
- a sanitized example;
- the smallest test that fails before and passes after the change;
- any accuracy, privacy, platform, or compatibility tradeoff.

Run before submitting:

```bash
python -m unittest discover -s tests -v
python -m compileall -q brain tests
git diff --check
```

By contributing, you agree that your contribution is licensed under the repository's
MIT license. Be respectful and constructive in all project spaces.
