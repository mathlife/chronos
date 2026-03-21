import importlib
import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import core.paths as paths

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_TMP_ROOT = PROJECT_ROOT / ".tmp_tests"
LOCAL_TMP_ROOT.mkdir(exist_ok=True)


def make_temp_dir() -> Path:
    path = LOCAL_TMP_ROOT / f"tmp_{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


class PathsTests(unittest.TestCase):
    def test_workspace_env_overrides_default_lookup(self):
        tmpdir = make_temp_dir()
        try:
            workspace = Path(tmpdir)
            (workspace / "todo.db").write_text("", encoding="utf-8")

            with patch.dict(os.environ, {"CHRONOS_WORKSPACE": str(workspace)}, clear=False):
                reloaded = importlib.reload(paths)
                self.assertEqual(reloaded.WORKSPACE, workspace)
                self.assertEqual(reloaded.TODO_DB, workspace / "todo.db")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        importlib.reload(paths)

    def test_db_path_env_has_highest_priority(self):
        tmpdir = make_temp_dir()
        try:
            workspace = Path(tmpdir)
            custom_db = workspace / "custom.db"

            with patch.dict(
                os.environ,
                {
                    "CHRONOS_WORKSPACE": str(workspace),
                    "CHRONOS_DB_PATH": str(custom_db),
                },
                clear=False,
            ):
                reloaded = importlib.reload(paths)
                self.assertEqual(reloaded.TODO_DB, custom_db)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        importlib.reload(paths)


if __name__ == "__main__":
    unittest.main()
