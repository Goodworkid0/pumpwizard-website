import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import deploy_release


class ReleaseGuardTests(unittest.TestCase):
    def test_unheld_lock_names_are_allowed_but_active_writer_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'data').mkdir()
            path=root/'data/test.sqlite.lock'; path.touch()
            with patch.object(deploy_release,'ROOT',root):
                deploy_release.no_writers()
                with path.open('rb') as held:
                    fcntl.flock(held,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    with self.assertRaises(BlockingIOError): deploy_release.no_writers()
                deploy_release.no_writers()

    def test_failed_commands_do_not_echo_provider_output(self):
        result=type('Result',(),{'returncode':1,'stdout':'secret-value','stderr':'secret-value'})()
        with patch('scripts.deploy_release.subprocess.run',return_value=result):
            with self.assertRaises(RuntimeError) as raised: deploy_release.run('example')
        self.assertNotIn('secret-value',str(raised.exception))

    def test_unrelated_or_missing_staging_process_is_not_waited_on(self):
        import os
        self.assertIsNone(deploy_release.proc_identity(os.getpid()))
        self.assertIsNone(deploy_release.proc_identity(99999999))
