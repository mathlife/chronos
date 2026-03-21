import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.paths as paths

class PathsTests(unittest.TestCase):
    def test_workspace_env_overrides_default_lookup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "todo.db").write_text("", encoding="utf-8")

            with patch.dict(os.environ, {"CHRONOS_WORKSPACE": str(workspace)}, clear=False):
                reloaded = importlib.reload(paths)
                self.assertEqual(reloaded.WORKSPACE, workspace)
                self.assertEqual(reloaded.TODO_DB, workspace / "todo.db")

        importlib.reload(paths)

    def test_db_path_env_has_highest_priority(self):
        with tempfile.TemporaryDirectory() as tmpdir:
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

        importlib.reload(paths)


if __name__ == "__main__":
    unittest.main()
