#!/bin/zsh
# Builds EmailAI.app (menu bar only, no Dock icon). Run from anywhere: ui/MenuBar/build_app.sh
set -e
cd "$(dirname "$0")"
swift build -c release
APP=EmailAI.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp .build/release/EmailAI "$APP/Contents/MacOS/"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>EmailAI</string>
  <key>CFBundleIdentifier</key><string>local.giu.emailai</string>
  <key>CFBundleExecutable</key><string>EmailAI</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
codesign --force -s - "$APP"  # ad-hoc: fine for an app only you run
echo "Built $(pwd)/$APP"
