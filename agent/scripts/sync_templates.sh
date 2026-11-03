#!/usr/bin/env bash
# Copy the canonical bench-template files (component A) into the agent package
# so they ship with the wheel: nexaploy_agent/templates/{bench,proxy,db,...}.
# Usage: agent/scripts/sync_templates.sh [path/to/bench-template]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="${1:-$here/../../bench-template}"
dst="$here/../nexaploy_agent/templates"

if [[ ! -f "$src/bench/docker-compose.yml.j2" ]]; then
    echo "error: $src does not look like bench-template/" >&2
    exit 1
fi
src="$(cd "$src" && pwd)"

mkdir -p "$dst/bench"
install -m 0644 "$src/bench/docker-compose.yml.j2" "$dst/bench/docker-compose.yml.j2"
[[ -f "$src/bench/env.example" ]] && install -m 0644 "$src/bench/env.example" "$dst/bench/env.example"
install -m 0644 "$src/site-route.yml.j2" "$dst/site-route.yml.j2"

for stack in proxy db; do
    if [[ -d "$src/$stack" ]]; then
        rm -rf "${dst:?}/$stack"
        mkdir -p "$dst/$stack"
        # Copy everything except real .env files / runtime data.
        (cd "$src/$stack" && find . -type f ! -name '.env' ! -path './data/*' ! -path './logs/*' -print0) |
            while IFS= read -r -d '' f; do
                mkdir -p "$dst/$stack/$(dirname "$f")"
                install -m 0644 "$src/$stack/$f" "$dst/$stack/$f"
            done
    fi
done

echo "templates synced from $src -> $(cd "$dst" && pwd)"
