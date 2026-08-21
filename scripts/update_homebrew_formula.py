#!/usr/bin/env python3
"""Render the Project Brain Homebrew formula from a final release SHA256SUMS."""

from __future__ import annotations

import argparse
from pathlib import Path


def checksums(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        if len(digest) == 64 and name:
            values[Path(name).name] = digest
    return values


def render(version: str, values: dict[str, str]) -> str:
    assets = {
        "macos-arm64": f"project-brain-v{version}-macos-arm64.tar.gz",
        "macos-amd64": f"project-brain-v{version}-macos-amd64.tar.gz",
        "linux-arm64": f"project-brain-v{version}-linux-arm64.tar.gz",
        "linux-amd64": f"project-brain-v{version}-linux-amd64.tar.gz",
    }
    missing = [name for name in assets.values() if name not in values]
    if missing:
        raise ValueError("final SHA256SUMS is missing standalone release assets: " + ", ".join(missing))
    url = "https://github.com/superorange0707/project-brain/releases/download"
    return f'''class ProjectBrain < Formula
  desc "Give any chat AI read-only, multi-repository codebase exploration"
  homepage "https://github.com/superorange0707/project-brain"
  license "MIT"

  on_macos do
    if Hardware::CPU.arm?
      url "{url}/v{version}/{assets["macos-arm64"]}"
      sha256 "{values[assets["macos-arm64"]]}"
    else
      url "{url}/v{version}/{assets["macos-amd64"]}"
      sha256 "{values[assets["macos-amd64"]]}"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "{url}/v{version}/{assets["linux-arm64"]}"
      sha256 "{values[assets["linux-arm64"]]}"
    else
      url "{url}/v{version}/{assets["linux-amd64"]}"
      sha256 "{values[assets["linux-amd64"]]}"
    end
  end

  def install
    bin.install "brain", "codebase-memory-mcp", "zoekt", "zoekt-index"
    doc.install "PROJECT_BRAIN_LICENSE", "CODEBASE_MEMORY_LICENSE", "CODEBASE_MEMORY_THIRD_PARTY_NOTICES.md"
    doc.install "ZOEKt_LICENSE", "ZOEKt_VERSION"
  end

  test do
    assert_match "brain {version}", shell_output("#{{bin}}/brain --version")
    assert_match "0.10.5", shell_output("#{{bin}}/codebase-memory-mcp --version 2>&1")
    assert_predicate bin/"zoekt", :executable?
    assert_predicate bin/"zoekt-index", :executable?
  end
end
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256sums", type=Path, required=True)
    parser.add_argument("--formula", type=Path, required=True)
    args = parser.parse_args()
    if not args.version or any(character not in "0123456789." for character in args.version):
        raise SystemExit("--version must be a numeric release version")
    args.formula.write_text(render(args.version, checksums(args.sha256sums)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
