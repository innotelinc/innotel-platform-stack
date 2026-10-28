#!/usr/bin/env bash
# Print every conflict region in the four ported admin pages.
set -u
BUILD=/root/bulwark-src-build-1.18.0
cd "$BUILD" || exit 1

for f in "app/(main)/admin/_pages/marketplace.tsx" \
         "app/(main)/admin/_pages/policy.tsx" \
         "app/(main)/admin/_pages/telemetry.tsx" \
         "app/(main)/admin/_pages/version.tsx"; do
  n=$(grep -c '^<<<<<<<' "$f" 2>/dev/null || true)
  [ "${n:-0}" -gt 0 ] || continue
  echo "================ $f  ($n conflicts) ================"
  awk '
    /^<<<<<<</ {inside=1; print NR": "$0; next}
    /^>>>>>>>/ {print NR": "$0; inside=0; print ""; next}
    inside     {print NR": "$0}
  ' "$f"
done
