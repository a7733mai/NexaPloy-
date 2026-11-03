#!/usr/bin/env bash
# Nexaploy image builder — builds one bench image for a Release Group.
# Contract: ARCHITECTURE.md §4. Run `build.sh --help` for usage.
#
# stdout: only the final image tag (last line), plus the command in --dry-run mode.
# stderr: progress + docker build output (also appended to --log when given).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSIONS_JSON="${NEXAPLOY_VERSIONS_JSON:-$SCRIPT_DIR/versions.json}"
CONTAINERFILE="${NEXAPLOY_CONTAINERFILE:-$SCRIPT_DIR/Containerfile}"
FRAPPE_DOCKER_URL="${FRAPPE_DOCKER_URL:-https://github.com/frappe/frappe_docker}"
FRAPPE_DOCKER_REF="${FRAPPE_DOCKER_REF:-main}"
# buildx | docker | auto
BUILDER="${NEXAPLOY_BUILDER:-auto}"

usage() {
  cat <<'EOF'
Usage: build.sh --major <14|15|16> --apps-json <file> --tag <repo:tag> [options]

Required:
  --major N              Frappe major version (key in versions.json)
  --apps-json FILE       JSON array of {"url": "...", "branch": "..."} (may be [])
  --tag REPO:TAG         image tag, e.g. nexaploy/bench:erp-v15-main-20261103T2130

Options (defaults come from versions.json for the given major):
  --frappe-branch B      frappe branch/tag (default: versions.json frappe_branch)
  --frappe-path URL      frappe repo (default: https://github.com/frappe/frappe)
  --python X.Y.Z         Python version of the base image
  --node X.Y.Z           Node.js version installed via nvm
  --debian NAME          Debian base (bookworm, trixie)
  --context DIR          frappe_docker checkout used as build context
                         (default: <script dir>/frappe_docker; cloned if missing,
                         ref from $FRAPPE_DOCKER_REF, default "main")
  --platform P           e.g. linux/amd64 or linux/arm64 (buildx only)
  --build-id ID          override build id label (default: parsed from tag)
  --release-group RG     override release group label (default: parsed from tag)
  --no-cache             build without cache
  --push                 push the image after building
  --log FILE             append build output to FILE (apps.json copied next to it)
  --dry-run              validate inputs and print the docker command, do not build
  -h, --help             show this help

Environment:
  FRAPPE_DOCKER_REF      branch/tag/commit of frappe_docker to clone (default: main)
  FRAPPE_DOCKER_URL      frappe_docker repo URL
  NEXAPLOY_BUILDER       buildx | docker | auto (default: auto)
  NEXAPLOY_VERSIONS_JSON alternate versions.json
  NEXAPLOY_CONTAINERFILE alternate Containerfile

Exit status 0 on success; the last stdout line is the image tag.
EOF
}

die() { echo "build.sh: error: $*" >&2; exit "${2:-2}"; }
info() { echo "build.sh: $*" >&2; [[ -n "${LOG:-}" && "${DRY_RUN:-0}" != 1 ]] && echo "build.sh: $*" >>"$LOG" || true; }

# ---------------------------------------------------------------- arguments
MAJOR="" APPS_JSON="" TAG="" FRAPPE_BRANCH="" FRAPPE_PATH="https://github.com/frappe/frappe"
PYTHON_VERSION="" NODE_VERSION="" DEBIAN_BASE="" CONTEXT="" PLATFORM="" BUILD_ID="" RELEASE_GROUP=""
PUSH=0 NO_CACHE=0 DRY_RUN=0 LOG=""

need_val() { [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || die "$1 requires a value"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --major)          need_val "$@"; MAJOR="$2"; shift 2 ;;
    --apps-json)      need_val "$@"; APPS_JSON="$2"; shift 2 ;;
    --tag)            need_val "$@"; TAG="$2"; shift 2 ;;
    --frappe-branch)  need_val "$@"; FRAPPE_BRANCH="$2"; shift 2 ;;
    --frappe-path)    need_val "$@"; FRAPPE_PATH="$2"; shift 2 ;;
    --python)         need_val "$@"; PYTHON_VERSION="$2"; shift 2 ;;
    --node)           need_val "$@"; NODE_VERSION="$2"; shift 2 ;;
    --debian)         need_val "$@"; DEBIAN_BASE="$2"; shift 2 ;;
    --context)        need_val "$@"; CONTEXT="$2"; shift 2 ;;
    --platform)       need_val "$@"; PLATFORM="$2"; shift 2 ;;
    --build-id)       need_val "$@"; BUILD_ID="$2"; shift 2 ;;
    --release-group)  need_val "$@"; RELEASE_GROUP="$2"; shift 2 ;;
    --log)            need_val "$@"; LOG="$2"; shift 2 ;;
    --push)           PUSH=1; shift ;;
    --no-cache)       NO_CACHE=1; shift ;;
    --dry-run)        DRY_RUN=1; shift ;;
    -h|--help)        usage; exit 0 ;;
    *)                die "unknown argument: $1 (see --help)" ;;
  esac
done

[[ -n "$MAJOR" ]]     || die "--major is required"
[[ -n "$APPS_JSON" ]] || die "--apps-json is required"
[[ -n "$TAG" ]]       || die "--tag is required"

# ---------------------------------------------------------------- json helpers (jq, python3 fallback)
have() { command -v "$1" >/dev/null 2>&1; }
if have jq; then JSON_TOOL=jq; elif have python3; then JSON_TOOL=python3; else die "need jq or python3"; fi

# versions_get <major> <field>  -> prints value or nothing
versions_get() {
  if [[ $JSON_TOOL == jq ]]; then
    jq -er --arg m "$1" --arg f "$2" '.[$m][$f] // empty | strings' "$VERSIONS_JSON" 2>/dev/null || true
  else
    python3 -I -c 'import json,sys
d=json.load(open(sys.argv[1])); v=d.get(sys.argv[2],{}).get(sys.argv[3])
print(v) if isinstance(v,str) else None' "$VERSIONS_JSON" "$1" "$2" 2>/dev/null || true
  fi
}
versions_has_major() {
  if [[ $JSON_TOOL == jq ]]; then
    jq -e --arg m "$1" 'has($m) and (.[$m]|type=="object")' "$VERSIONS_JSON" >/dev/null 2>&1
  else
    python3 -I -c 'import json,sys
d=json.load(open(sys.argv[1])); sys.exit(0 if isinstance(d.get(sys.argv[2]),dict) else 1)' "$VERSIONS_JSON" "$1" 2>/dev/null
  fi
}
versions_majors() {
  if [[ $JSON_TOOL == jq ]]; then jq -r 'keys|join(" ")' "$VERSIONS_JSON" 2>/dev/null || true
  else python3 -I -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))))' "$VERSIONS_JSON" 2>/dev/null || true; fi
}

# validate_apps_json <file>: prints a problem description and returns 1 if invalid.
# Rules: top-level array; each item an object with non-empty string url + branch;
# url is https/http/ssh/git or git@host:path; frappe itself must not be listed.
validate_apps_json() {
  if [[ $JSON_TOOL == jq ]]; then
    local out
    if ! out="$(jq -r '
      if type != "array" then "top-level value must be a JSON array"
      else
        [ to_entries[] | .key as $i | .value |
          if type != "object" then "item \($i): must be an object {url, branch}"
          elif (.url|type) != "string" or (.url|length) == 0 then "item \($i): url must be a non-empty string"
          elif (.branch|type) != "string" or (.branch|length) == 0 then "item \($i): branch must be a non-empty string"
          elif (.url|test("^(https?|ssh|git)://[^[:space:]]+$|^git@[^[:space:]:]+:[^[:space:]]+$")|not) then "item \($i): unsupported url \(.url|sub("//[^/@]*@";"//***@"))"
          elif (.branch|test("^[A-Za-z0-9][A-Za-z0-9._/-]*$")|not) then "item \($i): invalid branch \(.branch)"
          elif (.url|sub("\\.git$";"")|sub("/+$";"")|split("/")|last|split(":")|last) == "frappe" then "item \($i): do not list frappe itself; use --frappe-branch/--frappe-path"
          else empty end ] | first // empty
      end' "$1" 2>&1)"; then
      echo "not valid JSON"; return 1
    fi
    [[ -z "$out" ]] || { echo "$out"; return 1; }
  else
    python3 -I - "$1" <<'PY'
import json, re, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("not valid JSON"); sys.exit(1)
if not isinstance(d, list):
    print("top-level value must be a JSON array"); sys.exit(1)
url_re = re.compile(r"^(https?|ssh|git)://\S+$|^git@[^\s:]+:\S+$")
br_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
for i, it in enumerate(d):
    if not isinstance(it, dict):
        print(f"item {i}: must be an object {{url, branch}}"); sys.exit(1)
    u, b = it.get("url"), it.get("branch")
    if not isinstance(u, str) or not u:
        print(f"item {i}: url must be a non-empty string"); sys.exit(1)
    if not isinstance(b, str) or not b:
        print(f"item {i}: branch must be a non-empty string"); sys.exit(1)
    if not url_re.match(u):
        print(f"item {i}: unsupported url " + re.sub(r"//[^/@]*@", "//***@", u)); sys.exit(1)
    if not br_re.match(b):
        print(f"item {i}: invalid branch {b}"); sys.exit(1)
    name = re.sub(r"/+$", "", re.sub(r"\.git$", "", u)).split("/")[-1].split(":")[-1]
    if name == "frappe":
        print(f"item {i}: do not list frappe itself; use --frappe-branch/--frappe-path"); sys.exit(1)
PY
  fi
}

apps_json_has_credentials() { grep -Eq '://[^/"[:space:]]+@' "$1"; }

# redact_stream: strip credentials from clone URLs (https://x-access-token:T@github.com/...)
# and bare GitHub tokens in build output; `bench init --verbose` echoes git commands.
if sed -u '' </dev/null >/dev/null 2>&1; then SED_UNBUF=(sed -u); else SED_UNBUF=(sed); fi
redact_stream() {
  "${SED_UNBUF[@]}" -E \
    -e 's#([A-Za-z][A-Za-z0-9+.-]*://)[^/@[:space:]"'"'"']+@#\1***@#g' \
    -e 's#(gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}#***#g'
}

# ---------------------------------------------------------------- validation
[[ -f "$VERSIONS_JSON" ]] || die "versions file not found: $VERSIONS_JSON"
[[ "$MAJOR" =~ ^[0-9]+$ ]] || die "--major must be a number, got '$MAJOR'"
versions_has_major "$MAJOR" || die "unknown Frappe major '$MAJOR' (supported: $(versions_majors))"

[[ -f "$APPS_JSON" && -r "$APPS_JSON" ]] || die "apps.json not found or unreadable: $APPS_JSON"
if ! problem="$(validate_apps_json "$APPS_JSON")"; then
  die "invalid apps.json ($APPS_JSON): $problem"
fi

# Tag: [registry[:port]/]name[/name...]:tag — explicit tag required, no digests.
TAG_RE='^([a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?/)?[a-z0-9]+([._-]+[a-z0-9]+)*(/[a-z0-9]+([._-]+[a-z0-9]+)*)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'
[[ "$TAG" =~ $TAG_RE ]] || die "invalid --tag '$TAG' (expected e.g. nexaploy/bench:erp-v15-main-20261103T2130)"
TAG_REF="${TAG##*:}"

# Release group / build id from <release_group>-<build_id> (ARCHITECTURE.md §2)
if [[ -z "$RELEASE_GROUP" || -z "$BUILD_ID" ]]; then
  if [[ "$TAG_REF" =~ ^([a-z][a-z0-9-]{2,30})-([0-9]{8}T[0-9]{4,6}Z?)$ ]]; then
    : "${RELEASE_GROUP:=${BASH_REMATCH[1]}}"
    : "${BUILD_ID:=${BASH_REMATCH[2]}}"
  else
    : "${RELEASE_GROUP:=$TAG_REF}"
    : "${BUILD_ID:=$TAG_REF}"
  fi
fi

: "${FRAPPE_BRANCH:=$(versions_get "$MAJOR" frappe_branch)}"
: "${PYTHON_VERSION:=$(versions_get "$MAJOR" python)}"
: "${NODE_VERSION:=$(versions_get "$MAJOR" node)}"
: "${DEBIAN_BASE:=$(versions_get "$MAJOR" debian)}"
: "${DEBIAN_BASE:=bookworm}"
[[ -n "$FRAPPE_BRANCH" && -n "$PYTHON_VERSION" && -n "$NODE_VERSION" ]] \
  || die "versions.json entry for major $MAJOR is incomplete (need python, node, frappe_branch)"

[[ "$PYTHON_VERSION" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || die "invalid --python '$PYTHON_VERSION'"
[[ "$NODE_VERSION" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]]      || die "invalid --node '$NODE_VERSION'"
[[ "$DEBIAN_BASE" =~ ^[a-z]+$ ]]                      || die "invalid --debian '$DEBIAN_BASE'"
[[ "$FRAPPE_BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "invalid --frappe-branch '$FRAPPE_BRANCH'"
[[ "$FRAPPE_PATH" =~ ^(https?|ssh|git)://[^[:space:]]+$|^git@[^[:space:]:]+:[^[:space:]]+$ ]] \
  || die "invalid --frappe-path '$FRAPPE_PATH'"
[[ -z "$PLATFORM" || "$PLATFORM" =~ ^[a-z0-9]+/[a-z0-9]+(/[a-z0-9]+)?(,[a-z0-9]+/[a-z0-9]+(/[a-z0-9]+)?)*$ ]] \
  || die "invalid --platform '$PLATFORM'"
[[ -f "$CONTAINERFILE" ]] || die "Containerfile not found: $CONTAINERFILE"

: "${CONTEXT:=$SCRIPT_DIR/frappe_docker}"

# ---------------------------------------------------------------- builder selection
case "$BUILDER" in
  buildx|docker) ;;
  auto)
    if have docker && docker buildx version >/dev/null 2>&1; then BUILDER=buildx
    elif have docker; then BUILDER=docker
    elif [[ $DRY_RUN == 1 ]]; then BUILDER=buildx
    else die "docker not found" 3; fi ;;
  *) die "NEXAPLOY_BUILDER must be buildx, docker or auto" ;;
esac
[[ $BUILDER == buildx || -z "$PLATFORM" || "$PLATFORM" != *,* ]] || die "multi-platform builds need buildx"
if [[ $BUILDER == buildx && "$PLATFORM" == *,* && $PUSH != 1 ]]; then
  die "multi-platform builds cannot be loaded locally; add --push"
fi

# ---------------------------------------------------------------- build args
APPS_JSON_BASE64="$(base64 <"$APPS_JSON" | tr -d '\n')"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

BUILD_ARGS=(
  --build-arg "PYTHON_VERSION=$PYTHON_VERSION"
  --build-arg "NODE_VERSION=$NODE_VERSION"
  --build-arg "DEBIAN_BASE=$DEBIAN_BASE"
  --build-arg "FRAPPE_BRANCH=$FRAPPE_BRANCH"
  --build-arg "FRAPPE_PATH=$FRAPPE_PATH"
  --build-arg "FRAPPE_UID=3113"
  --build-arg "FRAPPE_GID=3113"
  --build-arg "FRAPPE_MAJOR=$MAJOR"
  --build-arg "RELEASE_GROUP=$RELEASE_GROUP"
  --build-arg "BUILD_ID=$BUILD_ID"
  --build-arg "BUILD_DATE=$BUILD_DATE"
  --build-arg "IMAGE_VERSION=$TAG_REF"
  --build-arg "FRAPPE_DOCKER_REF=$FRAPPE_DOCKER_REF"
  --build-arg "CACHE_BUST=$BUILD_ID"
)
[[ -z "${WKHTMLTOPDF_VERSION:-}" ]] || BUILD_ARGS+=(--build-arg "WKHTMLTOPDF_VERSION=$WKHTMLTOPDF_VERSION")
[[ -z "${BENCH_VERSION:-}" ]]       || BUILD_ARGS+=(--build-arg "BENCH_VERSION=$BENCH_VERSION")

CMD=(docker)
if [[ $BUILDER == buildx ]]; then
  CMD+=(buildx build --progress=plain)
  if [[ $PUSH == 1 ]]; then CMD+=(--push); else CMD+=(--load); fi
else
  CMD+=(build)
fi
CMD+=(--file "$CONTAINERFILE" --target backend --tag "$TAG")
[[ -z "$PLATFORM" ]] || CMD+=(--platform "$PLATFORM")
[[ $NO_CACHE == 0 ]] || CMD+=(--no-cache)
CMD+=("${BUILD_ARGS[@]}")
# The secret goes last so the printable form can redact it easily.
CMD+=(--build-arg "APPS_JSON_BASE64=$APPS_JSON_BASE64" "$CONTEXT")

# Printable command; base64 is redacted when apps.json embeds credentials.
printable_cmd() {
  local out="" a shown="$APPS_JSON_BASE64"
  if apps_json_has_credentials "$APPS_JSON"; then shown="<redacted: apps.json contains credentials>"; fi
  for a in "${CMD[@]}"; do
    [[ "$a" == APPS_JSON_BASE64=* ]] && a="APPS_JSON_BASE64=$shown"
    out+="$(printf '%q' "$a") "
  done
  printf '%s\n' "${out% }"
}

clone_cmd() {
  printf 'git clone --depth 1 --branch %q %q %q\n' "$FRAPPE_DOCKER_REF" "$FRAPPE_DOCKER_URL" "$CONTEXT"
}

if [[ $DRY_RUN == 1 ]]; then
  if [[ ! -d "$CONTEXT/resources" ]]; then
    echo "# context missing, would run: $(clone_cmd)"
  fi
  printable_cmd
  [[ $PUSH == 1 && $BUILDER == docker ]] && printf 'docker push %q\n' "$TAG"
  echo "$TAG"
  exit 0
fi

# ---------------------------------------------------------------- real build
have docker || die "docker not found" 3
have git || [[ -d "$CONTEXT/resources" ]] || die "git not found (needed to clone frappe_docker)" 3

if [[ -n "$LOG" ]]; then
  mkdir -p "$(dirname "$LOG")"
  ( umask 077; : >>"$LOG" )
  log_dir="$(cd "$(dirname "$LOG")" && pwd)"
  if [[ "$(cd "$(dirname "$APPS_JSON")" && pwd)/$(basename "$APPS_JSON")" != "$log_dir/apps.json" ]]; then
    # the copy next to the log never keeps clone credentials
    ( umask 077; redact_stream <"$APPS_JSON" >"$log_dir/apps.json" )
  fi
fi

# run_logged CMD... : output (redacted) to stderr and (if set) the log file
run_logged() {
  if [[ -n "$LOG" ]]; then "$@" 2>&1 | redact_stream | tee -a "$LOG" >&2
  else "$@" 2>&1 | redact_stream >&2; fi
}

info "major=$MAJOR frappe_branch=$FRAPPE_BRANCH python=$PYTHON_VERSION node=$NODE_VERSION debian=$DEBIAN_BASE"
info "release_group=$RELEASE_GROUP build_id=$BUILD_ID tag=$TAG builder=$BUILDER"

if [[ ! -d "$CONTEXT/resources" ]]; then
  [[ ! -e "$CONTEXT" ]] || [[ -z "$(ls -A "$CONTEXT" 2>/dev/null)" ]] \
    || die "context $CONTEXT exists but has no resources/ directory" 4
  info "cloning frappe_docker@$FRAPPE_DOCKER_REF into $CONTEXT"
  mkdir -p "$(dirname "$CONTEXT")"
  if [[ "$FRAPPE_DOCKER_REF" =~ ^[0-9a-f]{40}$ ]]; then
    run_logged git init -q "$CONTEXT"
    run_logged git -C "$CONTEXT" remote add origin "$FRAPPE_DOCKER_URL"
    run_logged git -C "$CONTEXT" fetch -q --depth 1 origin "$FRAPPE_DOCKER_REF"
    run_logged git -C "$CONTEXT" checkout -q FETCH_HEAD
  else
    run_logged git clone -q --depth 1 --branch "$FRAPPE_DOCKER_REF" "$FRAPPE_DOCKER_URL" "$CONTEXT"
  fi
  [[ -d "$CONTEXT/resources" ]] || die "cloned context has no resources/ directory" 4
fi

info "running: $(printable_cmd)"
if ! run_logged "${CMD[@]}"; then
  info "build FAILED for $TAG"
  exit 1
fi

if [[ $PUSH == 1 && $BUILDER == docker ]]; then
  info "pushing $TAG"
  run_logged docker push "$TAG" || { info "push FAILED for $TAG"; exit 1; }
fi

info "build OK: $TAG"
echo "$TAG"
