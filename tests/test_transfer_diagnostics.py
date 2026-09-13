import unittest
from lab_backup.transport import transfer_failure


class DiagnosticsTests(unittest.TestCase):
    def test_exception_messages_never_logged(self):
        class FloodWaitError(Exception): seconds=42
        error=ExceptionGroup('secret',[FloodWaitError('secret token'),TimeoutError('https://secret')])
        kind,delay=transfer_failure(error)
        self.assertEqual(kind,'FloodWaitError,TimeoutError')
        self.assertEqual(delay,42)
        self.assertNotIn('secret',kind)
