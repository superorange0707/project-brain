from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class InstallerTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX installer runs on macOS/Linux")
    def test_posix_installer_verifies_and_installs_all_adjacent_tools(self) -> None:
        if not all(shutil.which(name) for name in ("curl", "tar", "sh")):
            self.skipTest("standard standalone installer tools are unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "assets"
            package = root / "package"
            destination = root / "installed"
            assets.mkdir()
            package.mkdir()
            for name in ("brain", "codebase-memory-mcp", "zoekt", "zoekt-index"):
                content = "#!/bin/sh\nprintf 'brain 1.0.0\\n'\n" if name == "brain" else f"#!/bin/sh\nprintf '{name}\\n'\n"
                (package / name).write_text(content, encoding="utf-8")
            notices = {
                "PROJECT_BRAIN_LICENSE", "CODEBASE_MEMORY_LICENSE",
                "CODEBASE_MEMORY_THIRD_PARTY_NOTICES.md", "ZOEKt_LICENSE", "ZOEKt_VERSION",
            }
            for name in notices:
                (package / name).write_text(f"notice: {name}\n", encoding="utf-8")
            platform = "macos-arm64" if os.uname().sysname == "Darwin" and os.uname().machine == "arm64" else (
                "macos-amd64" if os.uname().sysname == "Darwin" else
                "linux-arm64" if os.uname().machine in {"arm64", "aarch64"} else "linux-amd64"
            )
            archive = assets / f"project-brain-v1.0.0-{platform}.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                for path in sorted(package.iterdir()):
                    output.add(path, arcname=path.name)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            (assets / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
            completed = subprocess.run(
                ["sh", str(ROOT / "scripts/install-project-brain.sh"), "--version", "1.0.0",
                 "--release-base-url", assets.as_uri(), "--install-dir", str(destination)],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(
                {"brain", "codebase-memory-mcp", "zoekt", "zoekt-index", *notices},
                {path.name for path in destination.iterdir() if not path.name.startswith(".")},
            )
            self.assertTrue(all(os.access(destination / name, os.X_OK)
                                for name in ("brain", "codebase-memory-mcp", "zoekt", "zoekt-index")))
            self.assertTrue(all((destination / name).is_file() for name in notices))
            self.assertTrue((destination / ".project-brain-managed/current").is_symlink())
            version_dir = (destination / ".project-brain-managed/current").resolve()
            (version_dir / "brain").write_text("CORRUPT\n", encoding="utf-8")
            repaired = subprocess.run(
                ["sh", str(ROOT / "scripts/install-project-brain.sh"), "--version", "1.0.0",
                 "--release-base-url", assets.as_uri(), "--install-dir", str(destination)],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertEqual(0, repaired.returncode, repaired.stderr)
            self.assertEqual("#!/bin/sh\nprintf 'brain 1.0.0\\n'\n", (destination / "brain").read_text(encoding="utf-8"))

            previous_target = (destination / ".project-brain-managed/current").resolve()
            (package / "brain").write_text("#!/bin/sh\nprintf 'brain 0.9.9\\n'\n", encoding="utf-8")
            mismatched_archive = assets / f"project-brain-v1.0.1-{platform}.tar.gz"
            with tarfile.open(mismatched_archive, "w:gz") as output:
                for path in sorted(package.iterdir()):
                    output.add(path, arcname=path.name)
            mismatch_digest = hashlib.sha256(mismatched_archive.read_bytes()).hexdigest()
            (assets / "SHA256SUMS.txt").write_text(
                f"{mismatch_digest}  {mismatched_archive.name}\n", encoding="utf-8",
            )
            rejected = subprocess.run(
                ["sh", str(ROOT / "scripts/install-project-brain.sh"), "--version", "1.0.1",
                 "--release-base-url", assets.as_uri(), "--install-dir", str(destination)],
                text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
            )
            self.assertNotEqual(0, rejected.returncode)
            self.assertIn("version mismatch", rejected.stderr)
            self.assertEqual(previous_target, (destination / ".project-brain-managed/current").resolve())
            self.assertEqual("brain 1.0.0\n", subprocess.check_output(
                [str(destination / "brain"), "--version"], text=True, encoding="utf-8",
            ))

    def test_release_workflow_publishes_and_smokes_native_installers(self) -> None:
        release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        powershell = (ROOT / "scripts/install-project-brain.ps1").read_text(encoding="utf-8")
        shell = (ROOT / "scripts/install-project-brain.sh").read_text(encoding="utf-8")
        for name in ("install-project-brain.sh", "install-project-brain.ps1"):
            self.assertIn(name, release)
        self.assertIn("SHA256SUMS.txt", powershell)
        self.assertIn("Get-FileHash", powershell)
        self.assertIn('[Alias("ZipPath")][string]$ArchivePath', powershell)
        self.assertIn('Copy-Item -LiteralPath $ArchivePath -Destination $archive', powershell)
        self.assertIn('Resolve-Path -LiteralPath $ChecksumPath', powershell)
        self.assertIn('duplicate entries for $archiveName', powershell)
        self.assertIn('offline upgrade version mismatch', release)
        self.assertIn('bad-checksum rollback did not preserve installation', release)
        self.assertNotIn("Set-ExecutionPolicy", powershell)
        self.assertIn("Move-Item -LiteralPath $stage -Destination $InstallDir", powershell)
        self.assertIn("if (-not $activated -and (Test-Path -LiteralPath $stage))", powershell)
        self.assertIn("Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue", powershell)
        self.assertIn("if ($stage -and (Test-Path -LiteralPath $stage))", powershell)
        self.assertIn("Project Brain was installed, but PATH could not be updated", powershell)
        self.assertIn('("$stagedVersion").Trim() -cne "brain $Version"', powershell)
        self.assertIn('$InstallDir = [IO.Path]::GetFullPath($InstallDir)', powershell)
        self.assertIn("install directory contains an unexpected entry", powershell)
        self.assertNotIn("Copy-Item -Destination $stage -Recurse", powershell)
        self.assertIn("SHA256SUMS.txt", shell)
        self.assertIn("sha256sum", shell)
        self.assertIn('[ "$staged_version" = "brain ${version}" ]', shell)
        self.assertIn('test "$("$INSTALL_DIR/brain" --version)" = "brain $VERSION"', release)
        self.assertIn('$installedVersion -cne "brain $version"', release)
        self.assertIn("cp scripts/install-project-brain.sh scripts/install-project-brain.ps1 dist/", release)
        self.assertIn("sha256sum *.whl *.tar.gz *.zip *.sh *.ps1 > SHA256SUMS.txt", release)
        self.assertIn("gh release download \"$GITHUB_REF_NAME\" --dir release-verification", release)
        self.assertIn("sha256sum -c SHA256SUMS.txt", release)
        self.assertIn("gh release edit \"$GITHUB_REF_NAME\" --draft=false", release)
        self.assertIn("actions/attest@v4", release)
        self.assertIn("attestations: write", release)
        self.assertEqual(1, release.count("attestations: write"))
        self.assertNotIn("Invoke-Expression", powershell)


if __name__ == "__main__":
    unittest.main()
