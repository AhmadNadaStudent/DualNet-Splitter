"""Compatibility entry point for previously installed autostart tasks.

The GUI now owns TUN and its health checks, with a permanent tray icon.
Existing tasks pointing at this script automatically use the same lifecycle.
"""


def main():
    from gui import run_application
    run_application(autostart=True)


if __name__ == "__main__":
    main()
