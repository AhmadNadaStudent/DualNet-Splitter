# Third-party software

DualNet uses the following independent projects. Their names and trademarks belong
to their respective owners. DualNet is not affiliated with these projects.

| Component | Purpose | Source and license |
| --- | --- | --- |
| sing-box 1.14.4 | Optional process-based TUN routing core | [SagerNet/sing-box](https://github.com/SagerNet/sing-box), GPL-3.0-or-later |
| Wintun 0.14.1 | Optional Windows virtual network adapter | [wintun.net](https://www.wintun.net/), signed DLL distribution license included in its official ZIP; source GPL-2.0 |
| Python / Tk | Runtime and desktop UI | [Python](https://www.python.org/psf/license/), PSF license; Tcl/Tk license |
| Pillow | Tray icon images | [Pillow](https://github.com/python-pillow/Pillow), HPND |
| pystray | Windows notification-area icon | [pystray](https://github.com/moses-palmer/pystray), LGPL-3.0 |
| colorama | Terminal colors | [colorama](https://github.com/tartley/colorama), BSD-3-Clause |
| PyInstaller | Executable packaging | [PyInstaller](https://pyinstaller.org/), GPL-2.0-or-later with bootloader exception |

The release ZIP excludes sing-box and Wintun binaries. When TUN mode is first
enabled, DualNet downloads these separately from their official upstream URLs.
Their original licenses continue to apply. Release packages include license
notices for bundled Python dependencies.

The Tcl 8.6.15 notice in `docs/licenses` comes from the
[official Tcl release source](https://github.com/tcltk/tcl/blob/core-8-6-15/license.terms).
Tk's notice is copied from the installed Python runtime into the release ZIP.

No project-wide redistribution license has been selected for DualNet's own source.
Public availability alone does not grant permission to redistribute or relicense it.
