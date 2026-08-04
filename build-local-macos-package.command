#!/bin/zsh
set -euo pipefail

# Build a local macOS package through the release wheel and PyInstaller pipeline.
ROOT="${0:A:h}"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "错误：这个脚本只能在 macOS 上运行。"
  exit 1
fi

for required_command in uv npm ditto codesign security unzip shasum; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    print -u2 "错误：缺少命令 $required_command，请先安装后重试。"
    exit 1
  fi
done

ARCHITECTURE="$(uname -m | tr '[:upper:]' '[:lower:]')"
case "$ARCHITECTURE" in
  arm64|x86_64) ;;
  *)
    print -u2 "错误：暂不支持这个 Mac 架构：$ARCHITECTURE"
    exit 1
    ;;
esac

# Prefer the stable local certificate and fall back to ad-hoc signing.
SIGNING_IDENTITY_NAME="CareerDesk Local Signing"
SIGNING_MODE="ad-hoc"
EXPECTED_SIGNATURE_MARK="Signature=adhoc"
typeset -a PACKAGE_SIGNING_ARGS
PACKAGE_SIGNING_ARGS=()
VALID_SIGNING_IDENTITIES="$(security find-identity -v -p codesigning 2>/dev/null || true)"
if [[ "$VALID_SIGNING_IDENTITIES" == *"\"$SIGNING_IDENTITY_NAME\""* ]]; then
  SIGNING_MODE="local-certificate"
  EXPECTED_SIGNATURE_MARK="Authority=$SIGNING_IDENTITY_NAME"
  PACKAGE_SIGNING_ARGS=(--codesign-identity "$SIGNING_IDENTITY_NAME")
fi

VERSION="$(awk -F '"' '/^version = "/ { print $2; exit }' backend/pyproject.toml)"
if [[ -z "$VERSION" ]]; then
  print -u2 "错误：无法从 backend/pyproject.toml 读取版本号。"
  exit 1
fi

TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
PACKAGE_NAME="CareerDesk-${VERSION}-macos-${ARCHITECTURE}-UNSIGNED-local-${TIMESTAMP}"
FINAL_DIRECTORY="$ROOT/$PACKAGE_NAME"
FINAL_ARCHIVE="$ROOT/$PACKAGE_NAME.zip"
if [[ -e "$FINAL_DIRECTORY" || -L "$FINAL_DIRECTORY" || -e "$FINAL_ARCHIVE" || -L "$FINAL_ARCHIVE" ]]; then
  print -u2 "错误：目标产物已经存在，脚本不会覆盖：$PACKAGE_NAME"
  exit 1
fi

STAGING_ROOT="$(mktemp -d "$ROOT/.careerdesk-local-package.XXXXXX")"
WORK_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/careerdesk-local-package.XXXXXX")"
cleanup() {
  rm -rf -- "$STAGING_ROOT" "$WORK_ROOT"
}
trap cleanup EXIT
trap 'print -u2 "\n打包失败：上方最后一条错误即失败原因；既有安装包没有被覆盖。"' ZERR

COMMIT="$(git rev-parse --short HEAD 2>/dev/null || print unknown)"
DIRTY_LABEL=""
if [[ -n "$(git status --porcelain --untracked-files=no 2>/dev/null || true)" ]]; then
  DIRTY_LABEL="（包含尚未提交的当前工作树改动）"
fi

print ""
print "CareerDesk 本地 Mac 安装包"
print "源码：$COMMIT $DIRTY_LABEL"
print "版本：$VERSION"
print "架构：$ARCHITECTURE"
print "签名：$SIGNING_MODE"
print "输出：$FINAL_ARCHIVE"
if [[ "$SIGNING_MODE" == "ad-hoc" ]]; then
  print "提示：先运行一次 ./创建本地Mac签名证书.command，之后的本地安装包在更新后不会再反复弹钥匙串授权。"
fi
print ""

print "[1/7] 安装锁定的前端依赖并构建前端…"
npm --prefix frontend ci
npm --prefix frontend run build

print "[2/7] 准备锁定的桌面构建环境…"
uv sync --project backend --group desktop-build --locked

print "[3/7] 从当前源码构建 CareerDesk wheel…"
WHEEL_DIRECTORY="$WORK_ROOT/wheel"
mkdir -m 700 "$WHEEL_DIRECTORY"
uv build --project backend --wheel --out-dir "$WHEEL_DIRECTORY"
WHEELS=("$WHEEL_DIRECTORY"/*.whl(N))
if (( ${#WHEELS[@]} != 1 )); then
  print -u2 "错误：预期生成且只生成一个 wheel，实际为 ${#WHEELS[@]} 个。"
  exit 1
fi

print "[4/7] 生成自包含 CareerDesk.app…"
DESKTOP_OUTPUT="$WORK_ROOT/desktop-output"
uv run --project backend --group desktop-build --locked python desktop/package_desktop.py \
  --wheel "$WHEELS[1]" \
  --output-dir "$DESKTOP_OUTPUT" \
  "${PACKAGE_SIGNING_ARGS[@]}"

APP="$DESKTOP_OUTPUT/dist/CareerDesk.app"
MANIFEST="$DESKTOP_OUTPUT/build-manifest.json"
codesign --verify --deep --strict "$APP"
SIGNATURE_DISPLAY="$(codesign --display --verbose=2 "$APP" 2>&1)"
if [[ "$SIGNATURE_DISPLAY" != *"$EXPECTED_SIGNATURE_MARK"* ]]; then
  print -u2 "错误：产物签名方式与预期不符（期望 $SIGNING_MODE）。实际签名信息："
  print -u2 -- "$SIGNATURE_DISPLAY"
  exit 1
fi

print "[5/7] 启动冻结应用并验证页面、数据库与备份恢复…"
uv run --project backend python scripts/frozen_artifact_smoke.py \
  --desktop-executable "$APP/Contents/MacOS/CareerDesk" \
  --data-executable "$APP/Contents/MacOS/careerdesk-data"

print "[6/7] 组装明确标记 UNSIGNED 的本地安装包…"
STAGED_DIRECTORY="$STAGING_ROOT/$PACKAGE_NAME"
STAGED_ARCHIVE="$STAGING_ROOT/$PACKAGE_NAME.zip"
mkdir -m 700 "$STAGED_DIRECTORY"
ditto "$APP" "$STAGED_DIRECTORY/CareerDesk.app"
cp "$MANIFEST" "$STAGED_DIRECTORY/build-manifest.json"
test "$(find "$STAGED_DIRECTORY" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = "2"

/usr/bin/python3 -c 'import json, pathlib, sys; data=json.loads(pathlib.Path(sys.argv[1]).read_text()); expected=(sys.argv[2], sys.argv[3], sys.argv[4]); actual=(data.get("version"), data.get("architecture"), data.get("code_signing")); assert actual == expected, (actual, expected)' \
  "$STAGED_DIRECTORY/build-manifest.json" "$VERSION" "$ARCHITECTURE" "$SIGNING_MODE"
codesign --verify --deep --strict "$STAGED_DIRECTORY/CareerDesk.app"
ditto -c -k --sequesterRsrc --keepParent "$STAGED_DIRECTORY" "$STAGED_ARCHIVE"
unzip -tq "$STAGED_ARCHIVE"

print "[7/7] 解压复核签名并发布到仓库根目录…"
VERIFY_DIRECTORY="$WORK_ROOT/archive-verify"
mkdir -m 700 "$VERIFY_DIRECTORY"
ditto -x -k "$STAGED_ARCHIVE" "$VERIFY_DIRECTORY"
codesign --verify --deep --strict "$VERIFY_DIRECTORY/$PACKAGE_NAME/CareerDesk.app"
test "$(find "$VERIFY_DIRECTORY/$PACKAGE_NAME" -mindepth 1 -maxdepth 1 | wc -l | tr -d ' ')" = "2"

mv "$STAGED_DIRECTORY" "$FINAL_DIRECTORY"
mv "$STAGED_ARCHIVE" "$FINAL_ARCHIVE"
ARCHIVE_SHA256="$(shasum -a 256 "$FINAL_ARCHIVE" | awk '{print $1}')"

print ""
print "打包成功。"
print "可直接运行：$FINAL_DIRECTORY/CareerDesk.app"
print "可保存或传输：$FINAL_ARCHIVE"
print "SHA-256：$ARCHIVE_SHA256"
print ""
if [[ "$SIGNING_MODE" == "local-certificate" ]]; then
  print "这是用本机自建证书（$SIGNING_IDENTITY_NAME）签名的 UNSIGNED 便捷包：跨版本更新不再反复弹钥匙串授权，但仍不是 Apple 公证安装包。"
else
  print "这是本机 ad-hoc 签名的 UNSIGNED 便捷包，不是 Apple 公证安装包。"
fi
