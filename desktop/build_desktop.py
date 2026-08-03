"""Build desktop launcher icons and the macOS app bundle."""

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
LOGO = ROOT / "frontend" / "public" / "logo-light-1024.png"


def _icon_base(size: int = 1024) -> Image.Image:
    """Render the logo on a rounded white RGBA icon canvas."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    margin = round(size * 0.098)
    radius = round(size * 0.185)
    draw.rounded_rectangle([margin, margin, size - margin, size - margin],
                           radius=radius, fill=(255, 255, 255, 255))
    glyph = Image.open(LOGO).convert("RGBA")
    glyph_size = round(size * 0.56)
    glyph = glyph.resize((glyph_size, glyph_size), Image.LANCZOS)
    offset = (size - glyph_size) // 2
    canvas.alpha_composite(glyph, (offset, offset))
    return canvas


def build_icns() -> Path | None:
    """Build the macOS icon when iconutil is available."""
    if not shutil.which("iconutil"):
        print("🔧 iconutil is unavailable; skipping CareerDesk.icns.")
        return None
    iconset = DESKTOP / "CareerDesk.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    for name, px in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64),
                     ("128x128", 128), ("128x128@2x", 256), ("256x256", 256),
                     ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)]:
        _icon_base(px).save(iconset / f"icon_{name}.png")
    icns = DESKTOP / "CareerDesk.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
    shutil.rmtree(iconset)
    return icns


def build_ico() -> Path:
    """Build the multi-size Windows icon."""
    ico = ROOT / "careerdesk.ico"
    _icon_base(256).save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return ico


APPLESCRIPT = '''on run
	set appPath to POSIX path of (path to me)
	set launcher to appPath & "../desktop/launch-headless.sh"
	do shell script "nohup /bin/zsh -l " & quoted form of launcher & " >/dev/null 2>&1 &"
end run'''


def build_app(icns: Path | None) -> Path | None:
    """Compile and ad-hoc sign the macOS AppleScript app bundle."""
    if not shutil.which("osacompile"):
        print("🔧 osacompile is unavailable; skipping CareerDesk.app.")
        return None
    if icns is None:
        sys.exit("iconutil is required to build CareerDesk.app.")
    app = ROOT / "CareerDesk.app"
    if app.exists():
        shutil.rmtree(app)
    script = DESKTOP / "_applet.applescript"
    script.write_text(APPLESCRIPT)
    subprocess.run(["osacompile", "-o", str(app), str(script)], check=True)
    script.unlink()

    contents = app / "Contents"
    shutil.copyfile(icns, contents / "Resources" / "applet.icns")
    assets = contents / "Resources" / "Assets.car"
    if assets.exists():
        assets.unlink()
    subprocess.run(["/usr/libexec/PlistBuddy", "-c", "Delete :CFBundleIconName",
                    str(contents / "Info.plist")], check=False)
    # Bundle mutations invalidate the compiler's ad-hoc signature.
    subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
    app.touch()
    return app


def main() -> None:
    if not LOGO.is_file():
        sys.exit(f"Missing logo source: {LOGO}")
    ico = build_ico()
    print(f"🔧 Generated icon: {ico.relative_to(ROOT)}")
    icns = build_icns()
    if icns:
        print(f"🔧 Generated icon: {icns.relative_to(ROOT)}")
    app = build_app(icns)
    if app:
        print(f"🔧 Built: {app.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
