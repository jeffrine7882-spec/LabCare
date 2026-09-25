#!/usr/bin/env bash
# Build the LabCare Android alerts app into a release APK without Gradle.
# Requires: JDK 11+, and the Android build-tools + platform android-33 in
# ~/android-sdk (see README.md). Output: dist/LabCare-Alerts-v1.3.apk
set -euo pipefail

# Make sure keytool/javac are on PATH regardless of the JDK install layout.
if ! command -v keytool >/dev/null 2>&1 && [ -d /usr/lib/jvm ]; then
    JAVAC_BIN="$(readlink -f "$(command -v javac)" 2>/dev/null || true)"
    [ -n "$JAVAC_BIN" ] && export PATH="$(dirname "$JAVAC_BIN"):$PATH"
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK="${ANDROID_SDK:-$HOME/android-sdk}"
# The SDK is a large build-only tool and is kept OUTSIDE the workspace (in
# /opt) so sandbox snapshots stay small; these fallbacks find it either way.
[ -d "$SDK" ] || SDK="/opt/android-sdk"
BT="$SDK/build-tools/android-13"
PLATFORM="$SDK/platform/android-13"
OUT="$APP_DIR/dist"
PKG="com.insforge.labcare"
SRC="$APP_DIR/src"
RES="$APP_DIR/res"
MANIFEST="$APP_DIR/AndroidManifest.xml"
WORK="$OUT/work"
KEYSTORE="$OUT/labcare.jks"

mkdir -p "$OUT" "$WORK/classes"

echo "==> compile resources"
"$BT/aapt2" compile --dir "$RES" -o "$WORK/res.zip"

echo "==> link resources"
"$BT/aapt2" link -o "$WORK/app.unsigned.apk" \
    -I "$PLATFORM/android.jar" \
    --manifest "$MANIFEST" \
    --java "$WORK/gen" \
    -A "$APP_DIR/assets" \
    "$WORK/res.zip"

echo "==> compile java"
find "$SRC" "$WORK/gen" -name '*.java' -print0 | \
    xargs -0 javac -encoding UTF-8 -source 8 -target 8 \
    -classpath "$PLATFORM/android.jar" \
    -d "$WORK/classes" 2>&1 | (grep -v "bootstrap class path" || true)

echo "==> dex"
"$BT/d8" --release --lib "$PLATFORM/android.jar" --output "$WORK" \
    "$WORK/classes/com/insforge/labcare/"*.class

echo "==> add classes.dex to APK"
cd "$WORK"
"$BT/aapt" add app.unsigned.apk classes.dex >/dev/null

echo "==> align"
"$BT/zipalign" -f 4 app.unsigned.apk app.aligned.apk

echo "==> keystore"
if [ ! -f "$KEYSTORE" ]; then
    keytool -genkeypair -keystore "$KEYSTORE" -alias labcare \
        -keyalg RSA -keysize 2048 -validity 10000 \
        -storepass labcare1 -keypass labcare1 \
        -dname "CN=LabCare, OU=InsForge, O=LabCare, L=Kuching, ST=Sarawak, C=MY"
fi

echo "==> sign"
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-key-alias labcare \
    --ks-pass pass:labcare1 --key-pass pass:labcare1 \
    --out "$OUT/LabCare-Alerts-v1.3.apk" app.aligned.apk

"$BT/apksigner" verify --print-certs "$OUT/LabCare-Alerts-v1.3.apk" | head -3

echo
echo "DONE: $OUT/LabCare-Alerts-v1.3.apk"
