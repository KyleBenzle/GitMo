#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION="0.1.2"
ARCH="all"
BUILD_DIR="$ROOT_DIR/build/deb/gitmo_${VERSION}_${ARCH}"
DEB_PATH="$ROOT_DIR/build/deb/gitmo_${VERSION}_${ARCH}.deb"

rm -rf "$BUILD_DIR"
mkdir -p \
  "$BUILD_DIR/DEBIAN" \
  "$BUILD_DIR/usr/bin" \
  "$BUILD_DIR/usr/lib/python3/dist-packages/gitmo" \
  "$BUILD_DIR/usr/share/applications" \
  "$BUILD_DIR/usr/share/doc/gitmo" \
  "$BUILD_DIR/usr/share/gitmo" \
  "$BUILD_DIR/usr/share/metainfo" \
  "$BUILD_DIR/usr/share/pixmaps"

cat > "$BUILD_DIR/DEBIAN/control" <<EOF
Package: gitmo
Version: $VERSION
Section: vcs
Priority: optional
Architecture: $ARCH
Maintainer: Kyle Benzle <kbe@gmx.us>
Depends: python3, python3-tk, python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, git, xdg-utils, libnotify-bin
Description: Automatic GitHub sync for local project folders
 GitMo is a Linux desktop app for keeping selected local folders synced
 to GitHub repositories with automatic commits and pushes.
EOF

cp "$ROOT_DIR"/gitmo/*.py "$BUILD_DIR/usr/lib/python3/dist-packages/gitmo/"
cp "$ROOT_DIR/logo.png" "$ROOT_DIR/icon.png" "$ROOT_DIR/logotrans.png" "$ROOT_DIR/logow.png" "$BUILD_DIR/usr/share/gitmo/"
cp "$ROOT_DIR/gui.png" "$ROOT_DIR/repogui.png" "$ROOT_DIR/README.md" "$BUILD_DIR/usr/share/doc/gitmo/"
cp "$ROOT_DIR/debian/com.kylebenzle.gitmo.desktop" "$BUILD_DIR/usr/share/applications/com.kylebenzle.gitmo.desktop"
cp "$ROOT_DIR/debian/com.kylebenzle.gitmo.metainfo.xml" "$BUILD_DIR/usr/share/metainfo/com.kylebenzle.gitmo.metainfo.xml"
cp "$ROOT_DIR/icon.png" "$BUILD_DIR/usr/share/pixmaps/gitmo.png"

cat > "$BUILD_DIR/usr/bin/gitmo" <<'EOF'
#!/usr/bin/env sh
exec python3 -m gitmo.app "$@"
EOF
chmod 755 "$BUILD_DIR/usr/bin/gitmo"

dpkg-deb --root-owner-group --build "$BUILD_DIR" "$DEB_PATH"
printf '%s\n' "$DEB_PATH"
