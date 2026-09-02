#!/bin/sh
set -eu

repository="superorange0707/project-brain"
version=""
install_dir="${HOME}/.local/bin"
release_base_url=""

usage() {
    echo "usage: sh install-project-brain.sh [--version X.Y.Z] [--install-dir DIR]" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version) version=${2-}; shift 2 ;;
        --install-dir) install_dir=${2-}; shift 2 ;;
        --repository) repository=${2-}; shift 2 ;;
        --release-base-url) release_base_url=${2-}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { echo "tar is required" >&2; exit 1; }

if [ -z "$version" ]; then
    [ -z "$release_base_url" ] || { echo "--version is required with --release-base-url" >&2; exit 2; }
    latest=$(curl -fsSIL -o /dev/null -w '%{url_effective}' "https://github.com/${repository}/releases/latest")
    version=${latest##*/v}
fi
printf '%s\n' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || {
    echo "release version must be X.Y.Z" >&2
    exit 2
}

case "$(uname -s):$(uname -m)" in
    Darwin:arm64|Darwin:aarch64) platform=macos-arm64 ;;
    Darwin:x86_64|Darwin:amd64) platform=macos-amd64 ;;
    Linux:arm64|Linux:aarch64) platform=linux-arm64 ;;
    Linux:x86_64|Linux:amd64) platform=linux-amd64 ;;
    *) echo "unsupported native platform: $(uname -s) $(uname -m)" >&2; exit 1 ;;
esac

tag="v${version}"
base=${release_base_url:-"https://github.com/${repository}/releases/download/${tag}"}
archive="project-brain-${tag}-${platform}.tar.gz"
temporary=$(mktemp -d "${TMPDIR:-/tmp}/project-brain-install.XXXXXX")
success=0
activation_started=0
old_current=""
rollback_dir="${temporary}/rollback"
cleanup() {
    if [ "$success" -ne 1 ] && [ "$activation_started" -eq 1 ]; then
        for managed_file in brain codebase-memory-mcp zoekt zoekt-index PROJECT_BRAIN_LICENSE CODEBASE_MEMORY_LICENSE CODEBASE_MEMORY_THIRD_PARTY_NOTICES.md ZOEKt_LICENSE ZOEKt_VERSION; do
            rm -f "${install_dir}/${managed_file}"
            if [ -f "${rollback_dir}/${managed_file}" ]; then
                cp -p "${rollback_dir}/${managed_file}" "${install_dir}/${managed_file}"
            fi
        done
        rm -f "${install_dir}/.project-brain-managed/current"
        if [ -n "$old_current" ]; then
            ln -s "$old_current" "${install_dir}/.project-brain-managed/current"
        fi
    fi
    rm -rf "$temporary"
}
trap cleanup EXIT HUP INT TERM

curl -fsSL "${base}/${archive}" -o "${temporary}/${archive}"
curl -fsSL "${base}/SHA256SUMS.txt" -o "${temporary}/SHA256SUMS.txt"
expected=$(awk -v target="$archive" '
    length($1) == 64 {
        name=$2; sub(/^\*/, "", name); sub(/^.*\//, "", name)
        if (name == target) { print tolower($1); exit }
    }
' "${temporary}/SHA256SUMS.txt")
[ -n "$expected" ] || { echo "published checksum is missing ${archive}" >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum "${temporary}/${archive}" | awk '{print tolower($1)}')
    file_sha256() { sha256sum "$1" | awk '{print tolower($1)}'; }
else
    actual=$(shasum -a 256 "${temporary}/${archive}" | awk '{print tolower($1)}')
    file_sha256() { shasum -a 256 "$1" | awk '{print tolower($1)}'; }
fi
[ "$actual" = "$expected" ] || { echo "Project Brain archive checksum mismatch" >&2; exit 1; }

tar -tzf "${temporary}/${archive}" > "${temporary}/members.txt"
while IFS= read -r member; do
    case "$member" in
        /*|../*|*/../*|*/..) echo "unsafe archive member: ${member}" >&2; exit 1 ;;
    esac
done < "${temporary}/members.txt"
mkdir "${temporary}/unpacked"
tar -xzf "${temporary}/${archive}" -C "${temporary}/unpacked"

for executable in brain codebase-memory-mcp zoekt zoekt-index; do
    [ -f "${temporary}/unpacked/${executable}" ] || { echo "archive is missing ${executable}" >&2; exit 1; }
done
for notice in PROJECT_BRAIN_LICENSE CODEBASE_MEMORY_LICENSE CODEBASE_MEMORY_THIRD_PARTY_NOTICES.md ZOEKt_LICENSE ZOEKt_VERSION; do
    [ -f "${temporary}/unpacked/${notice}" ] || { echo "archive is missing ${notice}" >&2; exit 1; }
done
mkdir -p "$install_dir"
managed_root="${install_dir}/.project-brain-managed"
versions="${managed_root}/versions"
version_name="${version}-${actual}"
version_dir="${versions}/${version_name}"
version_stage="${managed_root}/.version-${version_name}.$$"
mkdir -p "$versions" "$rollback_dir" "$version_stage"
for executable in brain codebase-memory-mcp zoekt zoekt-index; do
    install -m 0755 "${temporary}/unpacked/${executable}" "${version_stage}/${executable}"
    if [ -e "${install_dir}/${executable}" ]; then
        cp -p "${install_dir}/${executable}" "${rollback_dir}/${executable}"
    fi
done
for notice in PROJECT_BRAIN_LICENSE CODEBASE_MEMORY_LICENSE CODEBASE_MEMORY_THIRD_PARTY_NOTICES.md ZOEKt_LICENSE ZOEKt_VERSION; do
    install -m 0644 "${temporary}/unpacked/${notice}" "${version_stage}/${notice}"
    if [ -e "${install_dir}/${notice}" ]; then
        cp -p "${install_dir}/${notice}" "${rollback_dir}/${notice}"
    fi
done
staged_version=$("${version_stage}/brain" --version 2>&1) || {
    echo "staged Project Brain executable failed its version check" >&2
    exit 1
}
[ "$staged_version" = "brain ${version}" ] || {
    echo "staged Project Brain version mismatch: expected brain ${version}" >&2
    exit 1
}
existing_valid=0
if [ -d "$version_dir" ]; then
    existing_valid=1
    for executable in brain codebase-memory-mcp zoekt zoekt-index; do
        if [ ! -f "${version_dir}/${executable}" ] || [ -L "${version_dir}/${executable}" ] || [ ! -x "${version_dir}/${executable}" ] || \
           [ "$(file_sha256 "${version_dir}/${executable}")" != "$(file_sha256 "${version_stage}/${executable}")" ]; then
            existing_valid=0
            break
        fi
    done
    if [ "$existing_valid" -eq 1 ]; then
        for notice in PROJECT_BRAIN_LICENSE CODEBASE_MEMORY_LICENSE CODEBASE_MEMORY_THIRD_PARTY_NOTICES.md ZOEKt_LICENSE ZOEKt_VERSION; do
            if [ ! -f "${version_dir}/${notice}" ] || [ -L "${version_dir}/${notice}" ] || \
               [ "$(file_sha256 "${version_dir}/${notice}")" != "$(file_sha256 "${version_stage}/${notice}")" ]; then
                existing_valid=0
                break
            fi
        done
    fi
fi
if [ "$existing_valid" -eq 1 ]; then
    rm -rf "$version_stage"
elif [ ! -e "$version_dir" ]; then
    mv "$version_stage" "$version_dir"
else
    version_backup="${managed_root}/.corrupt-${version_name}.$$"
    mv "$version_dir" "$version_backup"
    if mv "$version_stage" "$version_dir"; then
        rm -rf "$version_backup"
    else
        mv "$version_backup" "$version_dir"
        exit 1
    fi
fi
if [ -L "${managed_root}/current" ]; then
    old_current=$(readlink "${managed_root}/current")
fi
replace_current_link() {
    if [ "$(uname -s)" = "Darwin" ]; then
        mv -f -h "$1" "${managed_root}/current"
    else
        mv -fT "$1" "${managed_root}/current"
    fi
}
if [ -z "$old_current" ]; then
    legacy_complete=1
    for executable in brain codebase-memory-mcp zoekt zoekt-index; do
        [ -f "${rollback_dir}/${executable}" ] || legacy_complete=0
    done
    initial_link="${managed_root}/.initial-current.$$"
    if [ "$legacy_complete" -eq 1 ]; then
        legacy_name="legacy-${actual}"
        legacy_dir="${versions}/${legacy_name}"
        mkdir -p "$legacy_dir"
        for executable in brain codebase-memory-mcp zoekt zoekt-index; do
            install -m 0755 "${rollback_dir}/${executable}" "${legacy_dir}/${executable}"
        done
        ln -s "versions/${legacy_name}" "$initial_link"
    else
        ln -s "versions/${version_name}" "$initial_link"
    fi
    mv "$initial_link" "${managed_root}/current"
fi
activation_started=1
for executable in brain codebase-memory-mcp zoekt zoekt-index; do
    link="${install_dir}/.${executable}.link.$$"
    ln -s ".project-brain-managed/current/${executable}" "$link"
    mv -f "$link" "${install_dir}/${executable}"
done
for notice in PROJECT_BRAIN_LICENSE CODEBASE_MEMORY_LICENSE CODEBASE_MEMORY_THIRD_PARTY_NOTICES.md ZOEKt_LICENSE ZOEKt_VERSION; do
    link="${install_dir}/.${notice}.link.$$"
    ln -s ".project-brain-managed/current/${notice}" "$link"
    mv -f "$link" "${install_dir}/${notice}"
done
current_link="${managed_root}/.current.$$"
ln -s "versions/${version_name}" "$current_link"
replace_current_link "$current_link"
success=1

echo "Installed Project Brain ${version} in ${install_dir}"
case ":${PATH}:" in
    *":${install_dir}:"*) ;;
    *) echo "Add ${install_dir} to PATH, then run: brain --version" ;;
esac
