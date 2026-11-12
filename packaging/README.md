# Packaging Forge

The repository is github.com/HSigei/forgeCLI.

## PyPI (`pipx install forgecommandline`)
`.github/workflows/publish.yml` runs on a `v*` tag: tests, version check, build, upload with PyPI
Trusted Publishing (no token stored). One-time setup on pypi.org: add a *pending publisher* for
project `forgecommandline`, owner/repo, workflow `publish.yml`, environment `pypi`.

## Windows exe (GitHub Releases)
`.github/workflows/release.yml` runs on the same tag and attaches `forge-vX.Y.Z-windows-x64.zip`
(a single `forge.exe`, no Python needed) plus its `.sha256`.

## Scoop
`scoop/forge.json` is a manifest with `checkver`/`autoupdate`, so it follows new releases. Put it in a
bucket repo (e.g. `HSigei/scoop-forge`, file `bucket/forge.json`), fill in the first hash, then users run:

    scoop bucket add forge https://github.com/HSigei/scoop-forge
    scoop install forge

## winget
winget manifests live in microsoft/winget-pkgs and are reviewed by Microsoft. After the first
release, from a Windows machine:

    winget install wingetcreate
    wingetcreate new https://github.com/HSigei/forgeCLI/releases/download/v0.8.6/forge-v0.8.6-windows-x64.zip

Use package id `HSigei.Forge`, mark it as a `portable` zip with `forge.exe` as the command. For later
versions: `wingetcreate update HSigei.Forge -u <new zip url> -v <version> --submit`.

## Homebrew
Once there are macOS/Linux users, add a tap with a formula that does `pip install forgecommandline` into a
virtualenv (`brew` "python" resource formula); not set up yet because the exe build is Windows-only.

## Docs site
`.github/workflows/docs.yml` publishes `docs/` to GitHub Pages on every push to `main`. Turn it on in
the repo: Settings → Pages → Source: GitHub Actions.
