# hedronos-branding — the look, from boot to desktop

One geometry (a stellated octahedron: two interlocking tetrahedra) drawn four ways: the
bootloader theme, the Plymouth splash, the four wallpapers, and the app icon. Palette:
`#5ad2a0` accent on `#0b0f14`, amber for warnings, red for failure.

## Files

| path | what it does |
|---|---|
| `/usr/share/hedron/branding/branding.json` | the single source of names/colours/asset paths every front end reads |
| `/usr/lib/hedron/hedron-branding` | CLI over it: `get`, `css`, `kdeglobals`, `colorscheme`, `wallpaper` |
| `/usr/share/hedron/wallpapers/*.svg` | 4 vector wallpapers (duality, nadir, terminus, umbra) |
| `/usr/share/plymouth/themes/hedronos/` | boot splash: turns, prints the boot question, turns **amber on failure** |
| `/etc/plymouth/plymouthd.conf` | `Theme=hedronos`, `ShowDelay=0`, show boot messages |
| `/etc/grub.d/00_header`, `/usr/share/hedron/grub/` | BIOS-boot fallback: same splash, menu forced after a failed boot |
| `/usr/share/icons/hicolor/scalable/apps/hedron-os.svg` | the logo, legible at 16 px |
| `/etc/issue`, `/etc/issue.net` | the login banner |

## Licensing

Code MIT; **artwork CC0-1.0** (`/usr/share/licenses/hedronos/README.md`) so a spin can reuse it
without attribution. No binary blobs are shipped: the splash draws the hedron with Cairo, the
wallpapers are vector, so `mkhedronpkg` can package everything from text.

## Try it

    hedron-branding get tagline
    hedron-branding colorscheme --name HedronOS > ~/.local/share/konsole/HedronOS.colors
    plymouthd --theme=hedronos &   # or: plymouth-set-default-theme -R hedronos
