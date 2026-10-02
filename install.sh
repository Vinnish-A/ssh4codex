#!/usr/bin/env bash
set -euo pipefail
bin_dir="${SSH4CODEX_BIN_DIR:-$HOME/.local/bin}"
lib_dir="${SSH4CODEX_LIB_DIR:-$(dirname -- "$bin_dir")/lib/ssh4codex}"
for dependency in curl tar sha256sum; do
  command -v "$dependency" >/dev/null || { printf 'Missing OS utility: %s\n' "$dependency" >&2; exit 1; }
done
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  printf 'This release supports Linux x86_64 / WSL, glibc 2.35+.\n' >&2; exit 1
fi
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/ssh4codex-install.XXXXXXXX")"
stage_dir=""
cleanup() {
  rm -rf -- "$work_dir"
  if [[ -n "$stage_dir" ]]; then rm -rf -- "$stage_dir"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
repo_url="https://github.com/Vinnish-A/ssh4codex"
version="${SSH4CODEX_VERSION:-}"
asset="ssh4codex-linux-x86_64.tar.gz"
if [[ -n "${SSH4CODEX_ARCHIVE:-}" ]]; then
  cp -- "$SSH4CODEX_ARCHIVE" "$work_dir/$asset"
  cp -- "$SSH4CODEX_ARCHIVE.sha256" "$work_dir/$asset.sha256"
else
  if [[ -z "$version" ]]; then
    release_url="$(curl -fsSL --connect-timeout 15 --max-time 60 -o /dev/null -w '%{url_effective}' "$repo_url/releases/latest")"
    version="${release_url##*/}"
  fi
  [[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { printf 'Invalid release version\n' >&2; exit 1; }
  base_url="$repo_url/releases/download/$version"
  curl -fsSL --connect-timeout 15 --max-time 180 "$base_url/$asset" -o "$work_dir/$asset"
  curl -fsSL --connect-timeout 15 --max-time 60 "$base_url/$asset.sha256" -o "$work_dir/$asset.sha256"
fi
(cd "$work_dir" && sha256sum --check "$asset.sha256")
mkdir "$work_dir/package"
tar -xzf "$work_dir/$asset" -C "$work_dir/package"
"$work_dir/package/ssh4codex" --version
package_version="$(cat "$work_dir/package/VERSION")"
version="${version:-v$package_version}"
[[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ && "$package_version" == "${version#v}" ]] || { printf 'Version mismatch\n' >&2; exit 1; }
mkdir -p "$bin_dir" "$lib_dir/releases"
bin_dir="$(cd "$bin_dir" && pwd)"
lib_dir="$(cd "$lib_dir" && pwd)"
release_dir="$lib_dir/releases/$version"
if [[ ! -d "$release_dir" ]]; then
  stage_dir="$(mktemp -d "$lib_dir/.install.XXXXXXXX")"
  cp -a "$work_dir/package/." "$stage_dir/"
  mv -- "$stage_dir" "$release_dir"; stage_dir=""
fi
stage_dir="$(mktemp -d "$lib_dir/.links.XXXXXXXX")"
ln -s "releases/$version" "$stage_dir/current"
mv -Tf -- "$stage_dir/current" "$lib_dir/current"
for program in ssh4codex ssh4codex-mcp; do
  links="$(mktemp -d "$bin_dir/.ssh4codex-link.XXXXXXXX")"
  ln -s "$lib_dir/current/$program" "$links/$program"
  mv -Tf -- "$links/$program" "$bin_dir/$program"
  rmdir "$links"
done
printf 'Installed ssh4codex %s to %s\n' "$version" "$bin_dir"
printf 'Usage: %s/ssh4codex --help\n' "$bin_dir"
case ":$PATH:" in *":$bin_dir:"*) ;; *) printf 'Add %s to PATH, or use the full command path.\n' "$bin_dir";; esac
