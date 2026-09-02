[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "ProjectBrain\bin"),
    [string]$Repository = "superorange0707/project-brain",
    [string]$ReleaseBaseUrl = "",
    [switch]$NoPathUpdate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne "X64") {
    throw "Project Brain currently supports native Windows x64 only"
}
if (-not $Version) {
    if ($ReleaseBaseUrl) { throw "-Version is required with -ReleaseBaseUrl" }
    $release = Invoke-RestMethod -Headers @{ "User-Agent" = "Project-Brain-Installer" } `
        -Uri "https://api.github.com/repos/$Repository/releases/latest"
    $Version = [string]$release.tag_name -replace '^v', ''
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "release version must be X.Y.Z" }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)

$tag = "v$Version"
$base = if ($ReleaseBaseUrl) { $ReleaseBaseUrl.TrimEnd('/') } else {
    "https://github.com/$Repository/releases/download/$tag"
}
$archiveName = "project-brain-$tag-windows-amd64.zip"
$temporary = Join-Path ([IO.Path]::GetTempPath()) ("project-brain-install-" + [Guid]::NewGuid())
$archive = Join-Path $temporary $archiveName
$checksums = Join-Path $temporary "SHA256SUMS.txt"
$unpacked = Join-Path $temporary "unpacked"
$stage = $null
$backup = $null
$activated = $false

New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    Invoke-WebRequest -UseBasicParsing -Uri "$base/$archiveName" -OutFile $archive
    Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS.txt" -OutFile $checksums
    $expected = $null
    foreach ($line in Get-Content -LiteralPath $checksums) {
        if ($line -match '^([0-9a-fA-F]{64})\s+\*?(.*)$' -and
            [IO.Path]::GetFileName($Matches[2]) -eq $archiveName) {
            $expected = $Matches[1].ToLowerInvariant()
            break
        }
    }
    if (-not $expected) { throw "published checksum is missing $archiveName" }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "Project Brain archive checksum mismatch" }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try {
        $root = [IO.Path]::GetFullPath($unpacked).TrimEnd('\') + '\'
        foreach ($entry in $zip.Entries) {
            $destination = [IO.Path]::GetFullPath((Join-Path $unpacked $entry.FullName))
            if (-not $destination.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
                throw "unsafe archive member: $($entry.FullName)"
            }
        }
    } finally {
        $zip.Dispose()
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $unpacked

    $managed = @("brain.exe", "codebase-memory-mcp.exe", "zoekt.exe", "zoekt-index.exe")
    $notices = @(
        "PROJECT_BRAIN_LICENSE", "CODEBASE_MEMORY_LICENSE", "CODEBASE_MEMORY_THIRD_PARTY_NOTICES.md",
        "ZOEKt_LICENSE", "ZOEKt_VERSION", "ZOEKt_WINDOWS_PATCH"
    )
    foreach ($name in @($managed + $notices)) {
        if (-not (Test-Path -LiteralPath (Join-Path $unpacked $name) -PathType Leaf)) {
            throw "archive is missing $name"
        }
    }

    $parent = Split-Path -Parent ([IO.Path]::GetFullPath($InstallDir))
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $stage = Join-Path $parent (".project-brain-stage-" + [Guid]::NewGuid())
    $backup = Join-Path $parent (".project-brain-backup-" + [Guid]::NewGuid())
    New-Item -ItemType Directory -Path $stage | Out-Null
    if (Test-Path -LiteralPath $InstallDir) {
        $allowed = @($managed + $notices)
        foreach ($entry in Get-ChildItem -LiteralPath $InstallDir -Force) {
            if ($entry.Name -notin $allowed -or $entry.PSIsContainer -or
                ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "install directory contains an unexpected entry; refusing to activate beside unverified content: $($entry.Name)"
            }
        }
    }
    foreach ($name in @($managed + $notices)) {
        Copy-Item -LiteralPath (Join-Path $unpacked $name) -Destination (Join-Path $stage $name) -Force
    }
    $stagedVersion = & (Join-Path $stage "brain.exe") --version
    if ($LASTEXITCODE -ne 0) { throw "staged Project Brain executable failed its version check" }
    if (("$stagedVersion").Trim() -cne "brain $Version") {
        throw "staged Project Brain version mismatch: expected brain $Version"
    }
    $movedOld = $false
    try {
        if (Test-Path -LiteralPath $InstallDir) {
            Move-Item -LiteralPath $InstallDir -Destination $backup
            $movedOld = $true
        }
        Move-Item -LiteralPath $stage -Destination $InstallDir
        $activated = $true
    } catch {
        if ($movedOld -and -not (Test-Path -LiteralPath $InstallDir)) {
            Move-Item -LiteralPath $backup -Destination $InstallDir
        }
        throw
    } finally {
        if (-not $activated -and (Test-Path -LiteralPath $stage)) {
            Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    # Activation is the commit point. A locked file in the retired directory
    # must not make a successful upgrade look failed or roll back the new tools.
    if ($movedOld -and (Test-Path -LiteralPath $backup)) {
        Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (-not $NoPathUpdate) {
        try {
            $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
            $entries = @($userPath -split ';' | Where-Object { $_ })
            if (-not ($entries | Where-Object { $_.TrimEnd('\') -ieq $InstallDir.TrimEnd('\') })) {
                [Environment]::SetEnvironmentVariable("Path", (($entries + $InstallDir) -join ';'), "User")
            }
            if (-not (($env:PATH -split ';') | Where-Object { $_.TrimEnd('\') -ieq $InstallDir.TrimEnd('\') })) {
                $env:PATH = "$InstallDir;$env:PATH"
            }
        } catch {
            Write-Warning "Project Brain was installed, but PATH could not be updated: $($_.Exception.Message)"
        }
    }
    Write-Host "Installed Project Brain $Version in $InstallDir"
    Write-Host "Run: brain.exe --version"
} finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue
    }
}
