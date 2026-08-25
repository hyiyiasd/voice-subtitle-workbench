from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from voice_subtitle_translator import APP_NAME
from voice_subtitle_translator.gui.main_window import MainWindow
from voice_subtitle_translator.logging_utils import configure_logging
from voice_subtitle_translator.paths import (
    AppPaths,
    PortableDirectoryError,
    configure_library_environment,
)


def main() -> int:
    if "--worker" in sys.argv:
        from voice_subtitle_translator.worker import main as worker_main

        return worker_main()
    if Path(sys.executable).stem.casefold() == "vst-cli" or "--cli" in sys.argv:
        from voice_subtitle_translator.cli import main as cli_main

        arguments = sys.argv[1:]
        if "--cli" in arguments:
            arguments.remove("--cli")
        return cli_main(arguments)
    application = QApplication(sys.argv)
    application.setApplicationName(APP_NAME)
    paths = AppPaths.discover()
    try:
        paths.ensure()
        configure_library_environment(paths)
        configure_logging(paths.logs)
    except PortableDirectoryError as exc:
        QMessageBox.critical(None, "程序目录不可写", str(exc))
        return 2
    window = MainWindow(paths)
    window.show()
    if os.environ.get("VST_SMOKE_TEST") == "1":
        QTimer.singleShot(500, application.quit)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
