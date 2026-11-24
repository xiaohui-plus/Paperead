# Code signing policy

## Project

- Name: Paperead
- Repository: https://github.com/xiaohui-plus/Paperead
- License: AGPL-3.0
- Maintainer: [@xiaohui-plus](https://github.com/xiaohui-plus)

Paperead is an open-source Windows desktop application for reading and
translating academic PDFs. It is built with Tauri 2 and a bundled Python
sidecar.

## Signing

We apply for free code signing for open-source projects through
[SignPath Foundation](https://signpath.org/). Until the application is
approved, release artifacts are unsigned and Windows may show an unknown
publisher warning.

Once approved, only the following artifacts are signed:

- `Paperead_<version>_x64-setup.exe` (NSIS installer)
- `paperead-desktop.exe` and `paperead-server.exe`
  (when the release pipeline can sign binaries before packaging)
- optionally the portable ZIP, if it is repackaged by SignPath

Artifacts are signed only when they are built by the GitHub Actions workflow
from a `v*` tag in this repository. We do not sign binaries built from
uncommitted local changes or from forks.

## Build provenance

- Build system: GitHub Actions, `windows-latest`
- Workflow: `.github/workflows/release.yml`
- Trigger: push of a `v*` tag, or an explicitly approved manual release
- Source: the commit referenced by the release tag
- Dependencies: pinned by `package-lock.json`, `Cargo.lock`, and the Python
  build environment created in CI

## Verification

After downloading a signed installer:

```powershell
Get-AuthenticodeSignature .\Paperead_0.1.0_x64-setup.exe
```

or:

```powershell
signtool verify /pa /v .\Paperead_0.1.0_x64-setup.exe
```

The signature should name SignPath Foundation as the signer and include a
timestamp. SHA256 checksums are published in `SHA256SUMS.txt` on the release
page.

## Reporting

If you believe a release artifact has been tampered with, please open an issue
at https://github.com/xiaohui-plus/Paperead/issues with the artifact name and
SHA256.
