import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from paper.package_registry import Packages, byte_range
from paper.public_read import ReadError
from scripts.publish_package import publish
from scripts.research_package import demo


class PackageTests(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(byte_range(None,100),(0,99))
        self.assertEqual(byte_range('bytes=20-',100),(20,99))
        self.assertEqual(byte_range('bytes=-20',100),(80,99))
        self.assertEqual(byte_range('bytes=0-200',100),(0,99))
        for value in ('bytes=100-','bytes=0-1,2-3','bytes=-0','bytes=5-1','bad'):
            with self.assertRaises(ReadError): byte_range(value,100)

    def test_review_required_and_only_registered_archives_served(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'package'; destination=root/'public'
            demo(source)
            with self.assertRaises(ValueError): publish(source,destination,'example')
            row=publish(source,destination,'example',reviewed=True)
            registry=Packages(destination)
            result=json.loads(registry.response({'instance':['example']}))
            self.assertEqual(len(result['packages']),1)
            metadata,stream=registry.open(row['sha256'])
            with stream: self.assertEqual(stream.read(2),b'PK')
            with self.assertRaises(ReadError): registry.open('../service.env')
            with self.assertRaises(ReadError): registry.open('a'*64)
            (destination/(row['sha256']+'.zip')).write_bytes(b'bad')
            with self.assertRaises(ReadError): registry.open(row['sha256'])

    def test_absent_registry_and_invalid_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            registry=Packages(temp); self.assertEqual(registry.records(),[])
            (Path(temp)/'registry.json').write_text('{}')
            with self.assertRaises(ReadError): registry.records()
