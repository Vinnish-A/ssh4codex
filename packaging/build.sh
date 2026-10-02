#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "${BASH_SOURCE[0]%/*}/.." && pwd)"
cd "$project_dir"
python_bin="${SSH4CODEX_BUILD_PYTHON:-python3}"
output_dir="${1:-$project_dir/release-assets}"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/ssh4codex-package.XXXXXXXX")"
trap 'rm -rf -- "$work_dir"' EXIT
"$python_bin" -m PyInstaller --noconfirm --clean --onedir --strip \
  --name ssh4codex --distpath "$work_dir/dist" --workpath "$work_dir/build" --specpath "$work_dir" \
  --paths "$project_dir" --collect-submodules mcp.server --collect-data jsonschema_specifications --copy-metadata mcp \
  --add-data "$project_dir/ssh4codex/remote.py:ssh4codex" \
  --add-data "$project_dir/ssh4codex/catalog.json:ssh4codex" \
  "$project_dir/packaging/entry.py"
package_dir="$work_dir/dist/ssh4codex"
mkdir -p "$package_dir/tools/lib" "$package_dir/licenses"
# Bundle the system OpenSSH executable and its non-glibc dependencies.
"$python_bin" - "$package_dir" <<'PY'
from pathlib import Path
import importlib.metadata
import re,shutil,subprocess,sys
package=Path(sys.argv[1])
ssh=Path('/usr/bin/ssh')
shutil.copy2(ssh,package/'tools/ssh.bin')
ldd=subprocess.check_output(['ldd',str(ssh)],text=True)
for path in re.findall(r'=>\s+(/\S+)',ldd):
 p=Path(path)
 if p.name in {'libc.so.6','libresolv.so.2','libpthread.so.0','libdl.so.2','librt.so.1'}:continue
 shutil.copy2(p,package/'tools/lib'/p.name)
for dist in importlib.metadata.distributions():
 name=dist.metadata['Name']
 for f in dist.files or []:
  if 'license' in str(f).lower() or Path(f).name.startswith(('COPYING','NOTICE')):
   src=Path(dist.locate_file(f))
   if src.is_file():shutil.copy2(src,package/'licenses'/(name+'-'+src.name))
for name in ['openssh-client','python3.10','libssl3','zlib1g','libselinux1','libpcre2-8-0','libkrb5-3','libkeyutils1','libcom-err2']:
 p=Path('/usr/share/doc')/name/'copyright'
 if p.is_file():shutil.copy2(p,package/'licenses'/(name+'.txt'))
PY
cat > "$package_dir/tools/ssh" <<'WRAPPER'
#!/bin/sh
base=$(CDPATH= cd -- "${0%/*}" && pwd)
LD_LIBRARY_PATH="$base/lib" exec "$base/ssh.bin" "$@"
WRAPPER
chmod 755 "$package_dir/tools/ssh"
cat > "$package_dir/ssh4codex-mcp" <<'WRAPPER'
#!/bin/sh
base=$(CDPATH= cd -- "${0%/*}" && pwd)
exec "$base/ssh4codex" mcp "$@"
WRAPPER
chmod 755 "$package_dir/ssh4codex-mcp"
cp LICENSE "$package_dir/LICENSE"
cp packaging/THIRD_PARTY.md "$package_dir/THIRD_PARTY.md"
cp README.md "$package_dir/README.md"
mkdir "$package_dir/docs"
cp docs/*.md "$package_dir/docs/"
mkdir "$package_dir/benchmarks"
for report in live_acceptance mcp_acceptance measurement stress_before stress_intermediate stress_after_final pressure32 stream_intermediate stream_drop rpc_stream_drop codex_analysis codex_handoff codex_network codex_multitask codex_multitask_intermediate codex_coordinator; do
  if [[ -f "benchmarks/$report.json" ]]; then
    cp "benchmarks/$report.json" "$package_dir/benchmarks/"
  fi
done
version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -1)"
printf '%s\n' "$version" > "$package_dir/VERSION"
asset="ssh4codex-linux-$(uname -m).tar.gz"
tar -czf "$output_dir/$asset" -C "$package_dir" .
(cd "$output_dir" && sha256sum "$asset" > "$asset.sha256")
printf 'Release archive: %s/%s\n' "$output_dir" "$asset"
