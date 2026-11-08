#!/bin/sh
# Builds "dist/Tube Loader.app" and dist/TubeLoader-mac.dmg on a Mac.
# Needs Python 3.9+ on the BUILD machine only; the finished app needs nothing.
# Use the python.org installer's Python (universal2) to get one app for both Apple Silicon and Intel,
# or set ARCH=arm64 / ARCH=x86_64 to build for one kind of Mac with any Python.
set -eu
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
ARCH="${ARCH:-universal2}"
APP="Tube Loader"
VERSION=$("$PY" -c 'import re; print(re.search(r"^VERSION = \"(.+?)\"", open("TubeLoader.pyw", encoding="utf-8").read(), re.M)[1])')

"$PY" -m venv --clear build/venv-mac
. build/venv-mac/bin/activate
python -m pip install --quiet --upgrade pip
python -m pip install --quiet --upgrade pyinstaller pywebview certifi

python -m PyInstaller --noconfirm --clean --windowed --name "$APP" --icon icon.icns \
  --osx-bundle-identifier io.github.builtbywestl.tubeloader --target-arch "$ARCH" \
  --hidden-import certifi --add-data "ui:ui" TubeLoader.pyw

PLIST="dist/$APP.app/Contents/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$PLIST"
plutil -replace CFBundleVersion -string "$VERSION" "$PLIST"
plutil -replace NSHumanReadableCopyright -string "MIT License. Copyright (c) 2026 BuiltByWestL" "$PLIST"
plutil -replace LSApplicationCategoryType -string "public.app-category.utilities" "$PLIST"
# Editing Info.plist breaks PyInstaller's signature: sign again (ad-hoc, there's no Apple developer ID)
codesign --force --deep --sign - "dist/$APP.app"
codesign --verify --deep --strict "dist/$APP.app"

rm -rf build/dmg dist/TubeLoader-mac.dmg
mkdir -p build/dmg
ditto "dist/$APP.app" "build/dmg/$APP.app"
ln -s /Applications build/dmg/Applications
hdiutil create -quiet -volname "$APP" -srcfolder build/dmg -fs HFS+ -format UDZO dist/TubeLoader-mac.dmg

echo
echo "Built: $(pwd)/dist/$APP.app ($(lipo -archs "dist/$APP.app/Contents/MacOS/$APP"))"
echo "       $(pwd)/dist/TubeLoader-mac.dmg"
