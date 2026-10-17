#!/usr/bin/env bash
# Build a hardened opencode: pinned upstream source + the patches in patches/ + a curated model catalogue.
#   ./build.sh [--models <models.dev api.json>] [--approved <approved-providers.json>] [--npm-registry <url>]
#              [--models-url <url>] [--out <dir>]
# Without --models the catalogue is downloaded from models.dev; on an offline build host, pass a reviewed copy.
# Needs git, node >= 18 and bun (the version in upstream's package.json).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=UPSTREAM
. "$HERE/UPSTREAM"

MODELS= APPROVED=$HERE/snapshot/approved-providers.json NPM_REGISTRY= MODELS_URL= OUT=$HERE/dist
while [ $# -gt 0 ]; do
  case $1 in
    --models) MODELS=$2; shift 2 ;;
    --approved) APPROVED=$2; shift 2 ;;
    --npm-registry) NPM_REGISTRY=$2; shift 2 ;;
    --models-url) MODELS_URL=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

WORK=${WORK:-$(mktemp -d)}
SRC=$WORK/opencode
echo "==> source: $UPSTREAM_REPO $UPSTREAM_TAG ($UPSTREAM_COMMIT)"
[ -d "$SRC" ] || git clone -q --depth 1 --branch "$UPSTREAM_TAG" "$UPSTREAM_REPO" "$SRC"
actual=$(git -C "$SRC" rev-parse HEAD)
[ "$actual" = "$UPSTREAM_COMMIT" ] || { echo "tag $UPSTREAM_TAG is $actual, expected $UPSTREAM_COMMIT: refusing to build" >&2; exit 1; }

echo "==> patches"
git -C "$SRC" -c user.name=build -c user.email=build@localhost am -q --committer-date-is-author-date "$HERE"/patches/*.patch
git -C "$SRC" log --oneline "$UPSTREAM_COMMIT"..HEAD

echo "==> model catalogue"
if [ -z "$MODELS" ]; then
  MODELS=$WORK/models-dev.json
  curl -fsSL https://models.dev/api.json -o "$MODELS"
fi
node "$HERE/snapshot/curate.mjs" "$MODELS" "$APPROVED" "$WORK/curated.json"

echo "==> dependencies"
(cd "$SRC" && bun install --frozen-lockfile)

echo "==> build"
HARDENED=$(node -e 'const [r, m] = process.argv.slice(1); console.log(JSON.stringify({ ...(r && { npmRegistry: r }), ...(m && { modelsUrl: m }) }))' \
  "$NPM_REGISTRY" "$MODELS_URL")
echo "    OPENCODE_HARDENED=$HARDENED"
(cd "$SRC/packages/opencode" &&
  OPENCODE_HARDENED=$HARDENED MODELS_DEV_API_JSON=$WORK/curated.json OPENCODE_VERSION=${UPSTREAM_TAG#v} \
    bun run script/build.ts --single --skip-install --skip-embed-web-ui)

mkdir -p "$OUT"
for bin in "$SRC"/packages/opencode/dist/*/bin/opencode*; do
  target=$(basename "$(dirname "$(dirname "$bin")")")
  cp "$bin" "$OUT/$target${bin##*/opencode}"
done
cp "$WORK/curated.json" "$OUT/models-catalogue.json"
(cd "$OUT" && sha256sum -- * | tee SHA256SUMS)
echo "==> built into $OUT"
