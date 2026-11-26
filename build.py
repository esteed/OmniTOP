"""Build dist\\omniTop.exe. Run build.cmd, which sets up the tools first.

Needs pywebview, pyinstaller and pillow in the running Python.
"""
from __future__ import annotations

import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(os.environ.get("LOCALAPPDATA") or HERE, "PortTrafficViz", "build")


def oklch(L: float, C: float, h: float) -> tuple[int, int, int]:
    """OKLCH -> sRGB, lowering chroma until it fits (same as the page's ramp)."""
    hr = math.radians(h)
    while True:
        a, b = C * math.cos(hr), C * math.sin(hr)
        l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
        m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
        s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
        lin = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
               -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
               -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
        if C <= 0 or all(-1e-4 <= x <= 1.0001 for x in lin):
            out = []
            for x in lin:
                x = min(1.0, max(0.0, x))
                x = 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
                out.append(round(255 * x))
            return tuple(out)
        C = max(0.0, C - 0.004)


# The icon: a ring split like the Memory page's memory bar (in use,
# kernel, modified, standby, free), around a performance graph, on a dark tile.
ICON_RING = [((0.68, 0.15, 152), 0.40), ((0.66, 0.17, 355), 0.15), ((0.84, 0.15, 78), 0.09),
             ((0.66, 0.14, 248), 0.24), ((0.40, 0.012, 240), 0.12)]
ICON_GRAPH = [(-1, 0.30), (-0.62, 0.05), (-0.34, 0.38), (-0.02, -0.42), (0.30, 0.12), (0.58, -0.18), (1, -0.02)]
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def icon_image(size: int):
    """One size of the icon, drawn 8x larger and scaled down for smooth edges.

    Strokes have a floor in output pixels, so small sizes stay legible.
    """
    from PIL import Image, ImageChops, ImageDraw

    ss = 8
    S = size * ss
    px = ss                                      # one output pixel
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = max(0.5 * px, S * 0.03)
    tile = (32, 32, 30, 255)
    d.rounded_rectangle((m, m, S - m, S - m), radius=S * 0.22, fill=tile,
                        outline=(70, 70, 66, 255), width=max(1, round(0.8 * px)))
    cx = cy = S / 2
    r1 = S * 0.38
    r0 = r1 - max(S * 0.10, 2.0 * px)
    gap = 0.0 if size <= 24 else 4.0             # degrees between the ring's parts

    def arc(a0: float, a1: float, steps: int = 96) -> list:
        outer = [(cx + r1 * math.cos(math.radians(a0 + (a1 - a0) * i / steps)),
                  cy + r1 * math.sin(math.radians(a0 + (a1 - a0) * i / steps))) for i in range(steps + 1)]
        inner = [(cx + r0 * math.cos(math.radians(a1 - (a1 - a0) * i / steps)),
                  cy + r0 * math.sin(math.radians(a1 - (a1 - a0) * i / steps))) for i in range(steps + 1)]
        return outer + inner

    a = -90.0
    for color, share in ICON_RING:
        span = 360 * share
        d.polygon(arc(a + gap / 2, a + span - gap / 2), fill=oklch(*color) + (255,))
        a += span

    w, h = r0 * 0.86, r0 * 0.62
    pts = [(cx + w * x, cy + h * y) for x, y in ICON_GRAPH]
    # The area under the graph, see-through, filling the inside of the ring
    fill = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    edge = [(cx + r0 * 1.2, pts[-1][1]), (cx + r0 * 1.2, cy + r0 * 1.2), (cx - r0 * 1.2, cy + r0 * 1.2),
            (cx - r0 * 1.2, pts[0][1])]
    ImageDraw.Draw(fill).polygon(pts + edge, fill=(246, 246, 242, 46))
    inside = Image.new("L", (S, S), 0)
    ri = r0 - max(S * 0.025, 0.7 * px)
    ImageDraw.Draw(inside).ellipse((cx - ri, cy - ri, cx + ri, cy + ri), fill=255)
    fill.putalpha(ImageChops.multiply(fill.getchannel("A"), inside))
    img.alpha_composite(fill)
    lw = max(S * 0.055, 1.5 * px)
    d.line(pts, fill=(246, 246, 242, 255), width=round(lw), joint="curve")
    for x, y in (pts[0], pts[-1]):               # round ends
        d.ellipse((x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2), fill=(246, 246, 242, 255))
    return img.resize((size, size), Image.LANCZOS)


def make_icon(path: str) -> None:
    """Write the icon with each size drawn separately."""
    images = [icon_image(s) for s in ICON_SIZES]
    images[-1].save(path, format="ICO", sizes=[(s, s) for s in ICON_SIZES], append_images=images[:-1])


def app_version() -> str:
    """VERSION from server.py, read as text so building doesn't import the app."""
    with open(os.path.join(HERE, "server.py"), encoding="utf-8") as f:
        m = re.search(r'^VERSION = "(\d+(?:\.\d+){0,3})"', f.read(), re.M)
    if not m:
        raise SystemExit('server.py has no VERSION = "x.y.z" line')
    return m.group(1)


def make_version_file(path: str, version: str) -> None:
    """The exe's version resource: Properties > Details shows it."""
    nums = tuple((list(map(int, version.split("."))) + [0, 0, 0])[:4])
    strings = {"FileDescription": "OmniTOP", "FileVersion": version, "InternalName": "omniTop",
               "OriginalFilename": "omniTop.exe", "ProductName": "OmniTOP", "ProductVersion": version}
    table = ",\n        ".join(f"StringStruct({k!r}, {v!r})" for k, v in strings.items())
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
        {table}])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")


def main() -> int:
    import PyInstaller.__main__

    os.makedirs(WORK, exist_ok=True)
    icon = os.path.join(WORK, "omniTop.ico")
    make_icon(icon)
    version = app_version()
    version_file = os.path.join(WORK, "version.txt")
    make_version_file(version_file, version)
    print(f"Building omniTop {version}")
    PyInstaller.__main__.run([
        os.path.join(HERE, "desktop.py"),
        "--name", "omniTop",
        "--onefile", "--windowed", "--noconfirm", "--clean",
        "--icon", icon,
        "--version-file", version_file,
        "--add-data", f"{os.path.join(HERE, 'web')}{os.pathsep}web",
        "--add-data", f"{os.path.join(HERE, 'linux')}{os.pathsep}linux",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(WORK, "work"),
        "--specpath", WORK,
    ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
