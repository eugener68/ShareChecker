#!/usr/bin/env bash
set -euo pipefail

APP="dist/ShareChecker.app"
NOTARIZE=false
NOTARY_PROFILE_DEFAULT="notary-profile"
NOTARY_PROFILE="${NOTARY_PROFILE:-$NOTARY_PROFILE_DEFAULT}"
IDENTITY_DEFAULT="Developer ID Application: Eugene Roitberg (HBY3U59ZBD)"
IDENTITY="${CODESIGN_IDENTITY:-$IDENTITY_DEFAULT}"

for arg in "$@"; do
  case "$arg" in
    --notarize)
      NOTARIZE=true
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--notarize]" >&2
      exit 2
      ;;
  esac
done

if [[ ! -d ".venv312" ]]; then
  echo "Missing .venv312. Create it with: python3.12 -m venv .venv312" >&2
  exit 1
fi

source .venv312/bin/activate

/bin/rm -rf ../dist ../build
python setup.py py2app

xattr -dr com.apple.quarantine "$APP" || true

while IFS= read -r -d '' f; do
  if file -b --mime-type "$f" | grep -q "application/x-mach-binary"; then
    codesign --remove-signature "$f" || true
    codesign --force --options runtime --timestamp --sign "$IDENTITY" "$f"
  fi
done < <(find "$APP" -type f -print0)

codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP/Contents/MacOS/python"
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP/Contents/MacOS/ShareChecker"

codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP"

codesign --verify --deep --strict "$APP"
spctl -a -vv "$APP" || true

if [[ "$NOTARIZE" == "true" ]]; then
  ZIP_PATH="dist/ShareChecker.zip"
  /bin/rm -f "$ZIP_PATH"
  ditto -c -k --keepParent "$APP" "$ZIP_PATH"
  xcrun notarytool submit "$ZIP_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$APP"
  spctl -a -vv "$APP" || true
  echo "Notarized and stapled: $APP"
fi

echo "Built and signed: $APP"
