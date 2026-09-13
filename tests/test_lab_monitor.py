import unittest
from collections import namedtuple
from lab_backup.capacity import budget
from scripts.monitor_lab import alert_transition


class MonitorTests(unittest.TestCase):
    def test_next_backup_capacity_keeps_reserve_and_first_restore_margin(self):
        Usage=namedtuple('Usage','total used free')
        usage=Usage(1000,600,400)
        ordinary=budget(100,usage,50)
        self.assertTrue(ordinary['sufficient'])
        self.assertEqual(ordinary['required_free_bytes'],330)
        first=budget(100,usage,50,True)
        self.assertFalse(first['sufficient'])
        self.assertEqual(first['shortfall_bytes'],150)
        self.assertFalse(budget(100,usage,200)['sufficient'])
        exact=budget(100,Usage(1000,670,330),50)
        self.assertTrue(exact['sufficient'])
        self.assertFalse(budget(100,Usage(1000,671,329),50)['sufficient'])

    def test_debounce_remind_and_recover(self):
        sent=[]; state={}
        for now in (100,220):
            state=alert_transition(state,{'readiness':False},now,sent.append)
        self.assertEqual(sent,[])
        state=alert_transition(state,{'readiness':False},340,sent.append)
        self.assertEqual(len(sent),1)
        state=alert_transition(state,{'readiness':False},460,sent.append)
        self.assertEqual(len(sent),1)
        state=alert_transition(state,{'readiness':False},3940,sent.append)
        self.assertEqual(len(sent),2)
        state=alert_transition(state,{'readiness':True},4000,sent.append)
        self.assertEqual(len(sent),3)
        state=alert_transition(state,{'readiness':True},4100,sent.append)
        self.assertEqual(len(sent),3)

    def test_changes_reset_debounce_and_failed_delivery_is_not_acknowledged(self):
        sent=[]
        state=alert_transition({}, {'disk':False},100,sent.append)
        state=alert_transition(state, {'disk':False},200,sent.append)
        state=alert_transition(state, {'disk':True},300,sent.append)
        self.assertEqual(sent,[])
        for now in (400,500): state=alert_transition(state,{'disk':False},now,sent.append)
        before=dict(state)
        def failed(_): raise OSError('offline')
        with self.assertRaises(OSError): alert_transition(state,{'disk':False},600,failed)
        self.assertEqual(state,before)
        state=alert_transition(state,{'disk':False},601,sent.append)
        self.assertEqual(len(sent),1)
