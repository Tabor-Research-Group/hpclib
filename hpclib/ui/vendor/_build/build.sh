#!/bin/sh
# Rebuilds ../codemirror6.js (and prints its sha384, which libs.json pins; update_libs.py does that too) from
# cm6-entry.js and the exact versions in package.json. Needs node and npm and the network; the page does not.
#
#     sh hpclib/ui/vendor/_build/build.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=$(cd "$here/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp "$here/package.json" "$here/package-lock.json" "$here/cm6-entry.js" "$work/"
cd "$work"
npm ci --no-audit --no-fund --ignore-scripts >/dev/null
npx esbuild cm6-entry.js --bundle --minify --format=iife --target=es2020 --legal-comments=none \
    --log-level=warning --outfile="$out/codemirror6.js"
printf 'sha384-%s\n' "$(openssl dgst -sha384 -binary "$out/codemirror6.js" | openssl base64 -A)"
