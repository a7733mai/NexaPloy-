#!/usr/bin/env bash
# Cut a Nexaploy release: bump every version string, date the CHANGELOG section,
# commit "Release vX.Y.Z" and create the annotated tag vX.Y.Z. Nothing is pushed.
#
#   scripts/release.sh 0.2.0            # or 0.2.0-beta.1 (a pre-release)
#   git push origin HEAD --follow-tags  # starts .github/workflows/release.yml
#
# Versions kept in sync: agent/nexaploy_agent/__init__.py, agent/pyproject.toml,
# control-plane/nexaploy/__init__.py (see scripts/release_utils.py).
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/release.sh [options] <version>

  --no-commit     only edit the files (no commit, no tag)
  --no-tag        commit but do not tag
  --date DATE     release date for the CHANGELOG (default: today, UTC)
  --allow-dirty   do not require a clean working tree
  -h, --help      show this help
EOF
}

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

COMMIT=1
TAG=1
DIRTY_OK=0
DATE="$(date -u +%Y-%m-%d)"
VERSION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-commit)   COMMIT=0; TAG=0; shift ;;
        --no-tag)      TAG=0; shift ;;
        --date)        [[ $# -ge 2 ]] || die "--date needs a value"; DATE="$2"; shift 2 ;;
        --allow-dirty) DIRTY_OK=1; shift ;;
        -h|--help)     usage; exit 0 ;;
        -*)            usage >&2; die "unknown option: $1" ;;
        *)             [[ -z "$VERSION" ]] || die "one version only"; VERSION="${1#v}"; shift ;;
    esac
done
[[ -n "$VERSION" ]] || { usage >&2; exit 2; }
[[ "$DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "--date must be YYYY-MM-DD"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UTILS="$ROOT/scripts/release_utils.py"
cd "$ROOT"

python3 "$UTILS" prerelease "$VERSION" >/dev/null || die "invalid version: $VERSION"

if [[ $COMMIT -eq 1 ]]; then
    git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository (use --no-commit)"
    if [[ $DIRTY_OK -eq 0 && -n "$(git status --porcelain)" ]]; then
        die "working tree is not clean (commit or stash first, or --allow-dirty)"
    fi
    if git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
        die "tag v$VERSION already exists"
    fi
fi

python3 "$UTILS" bump "$ROOT" "$VERSION"
python3 "$UTILS" changelog "$ROOT/CHANGELOG.md" "$VERSION" "$DATE"
python3 "$UTILS" check "$ROOT" "$VERSION"
echo "CHANGELOG: [$VERSION] - $DATE"

if [[ $COMMIT -eq 0 ]]; then
    echo "files updated; review with: git diff"
    exit 0
fi

git add CHANGELOG.md agent/nexaploy_agent/__init__.py agent/pyproject.toml control-plane/nexaploy/__init__.py
git commit -q -m "Release v$VERSION"
echo "committed: Release v$VERSION"
if [[ $TAG -eq 1 ]]; then
    python3 "$UTILS" notes "$ROOT/CHANGELOG.md" "$VERSION" | git tag -a "v$VERSION" -F -
    echo "tagged v$VERSION"
fi
echo
echo "Push to publish (runs .github/workflows/release.yml):"
echo "  git push origin HEAD --follow-tags"
