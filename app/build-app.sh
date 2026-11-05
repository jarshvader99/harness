#!/bin/bash
# Build TurboHarness and wrap it in a double-clickable .app bundle.
# Usage: ./build-app.sh   ->  dist/TurboHarness.app
set -euo pipefail
cd "$(dirname "$0")"

swift build -c release

# The icon is generated, not stored: rebuild it only when it is missing.
if [ ! -f AppIcon.icns ] || [ make-icon.py -nt AppIcon.icns ]; then
    python3 make-icon.py
fi

APP="dist/TurboHarness.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/TurboHarness "$APP/Contents/MacOS/TurboHarness"
cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>TurboHarness</string>
    <key>CFBundleIdentifier</key><string>local.turboharness.app</string>
    <key>CFBundleName</key><string>TurboHarness</string>
    <key>CFBundleDisplayName</key><string>TurboHarness</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "built $(pwd)/$APP"
