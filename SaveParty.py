"""SaveParty launcher - run with `python SaveParty.py`, or build an exe:

    pyinstaller --onefile --windowed --name SaveParty --collect-all customtkinter SaveParty.py
"""

import sys

if sys.version_info < (3, 10):
    sys.exit("SaveParty needs Python 3.10 or newer (found %s.%s)." % sys.version_info[:2])

try:
    from saveparty.ui.app import run
except ImportError as exc:
    sys.exit(
        f"Missing dependency: {getattr(exc, 'name', exc)}.\n"
        "Install requirements first:  pip install -r requirements.txt"
    )

if __name__ == "__main__":
    # Developer: publish a new build to the shared folder's update channel.
    #   python SaveParty.py --publish-update --version 1.1.0 --exe dist\SaveParty.exe [--mandatory] [--notes "..."]
    if "--publish-update" in sys.argv:
        from saveparty.update import publish_main

        sys.exit(publish_main(sys.argv[1:]))
    sys.exit(run(smoke="--smoke" in sys.argv))
