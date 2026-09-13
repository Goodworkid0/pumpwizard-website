import argparse
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lab_backup.common import save, load
from scripts.pause_telegram_backups import pause
from scripts.telegram_backup import run, main
from scripts.monitor_lab import collect


class BackupPauseTests(unittest.TestCase):
    def test_pause_preserves_private_configuration_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.json'
            original={'key':'untouched','credentials':'untouched','local_retention':'latest_verified'}
            save(path,original)
            first=pause(path)
            self.assertEqual(pause(path),first)
            result=load(path,private=True)
            for key,value in original.items(): self.assertEqual(result[key],value)
            self.assertIs(result['backup_enabled'],False)

    def test_all_new_backup_commands_skip_before_secrets_or_network(self):
        for command in ('daily','smoke','upload'):
            with self.subTest(command=command), patch('scripts.telegram_backup.load',side_effect=AssertionError('Secret read')):
                asyncio.run(run(argparse.Namespace(command=command),{'backup_enabled':False}))

    def test_cli_does_not_mutate_progress_or_acquire_transfer_lock_when_paused(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.json'; save(path,{'backup_enabled':False})
            with patch('sys.argv',['telegram_backup.py','--config',str(path),'daily','--apply']), \
                 patch('scripts.telegram_backup.lock',side_effect=AssertionError('Transfer lock')):
                self.assertEqual(main(),0)
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_paused_backups_keep_real_disk_and_readiness_failures_visible(self):
        for readiness,disk in ((True,True),(False,True),(True,False)):
            health={'checks':{'readiness':readiness,'disk':disk},'details':{},
                    'status':'ok' if readiness and disk else 'attention'}
            with patch('scripts.monitor_lab.check',return_value=copy.deepcopy(health)) as check, \
                 patch('scripts.monitor_lab.plan',side_effect=AssertionError('Backup inventory')):
                result=collect({'backup_enabled':False},10)
            self.assertEqual(result['checks'],health['checks'])
            self.assertEqual(result['status'],health['status'])
            self.assertEqual(result['details']['backups']['status'],'disabled_by_operator')
            self.assertEqual(len(check.call_args.args),2)
