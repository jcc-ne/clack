# Releasing clack-tui

## Manual dry run with TestPyPI

Create a TestPyPI account, enable 2FA, and create a TestPyPI API token.

Store the token in `~/.pypirc`:

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
username = __token__
password = pypi-REPLACE_ME

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-REPLACE_ME
```

Lock the file down:

```bash
chmod 600 ~/.pypirc
```

Build and upload:

```bash
uv build
uvx twine check dist/*
uvx twine upload -r testpypi dist/*
```

Smoke-test install from TestPyPI:

```bash
python3 -m venv /tmp/clack-test
source /tmp/clack-test/bin/activate
python -m pip install --upgrade pip
python -m pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple clack-tui
clack
```

If `0.1.0` has already been uploaded to TestPyPI, bump `version` in `pyproject.toml` before uploading again.

## Trusted Publishing to PyPI

The repo includes a GitHub Actions workflow at `.github/workflows/publish.yml` that publishes on GitHub Release publication.

Configure PyPI Trusted Publishing for the project:

1. Create the `clack-tui` project on PyPI if it does not exist yet, either by an initial manual upload or by creating the project through PyPI's publisher flow.
2. In PyPI, open the project, then go to `Manage` -> `Publishing`.
3. Add a GitHub publisher with:
   - Owner: `jcc-ne`
   - Repository: `clack`
   - Workflow filename: `publish.yml`
   - Environment name: `pypi`
4. In GitHub, create an environment named `pypi` for the repository. Add approval rules if you want a manual gate.
5. Publish a GitHub Release. The workflow will build with `uv build` and publish to PyPI via OIDC.

Notes:

- Trusted Publishing does not require a PyPI API token in GitHub secrets.
- The workflow requests `id-token: write`, which PyPI uses to mint a short-lived upload token.
- You can keep using the manual TestPyPI flow locally even after enabling Trusted Publishing for production releases.
