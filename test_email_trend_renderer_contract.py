import hashlib
import inspect
import unittest

from email_notifier import build_trend_png


ACTIVE_RENDERER_SOURCE_SHA256 = (
    "21f2ba56d736dc088f9df3d8457332cb542c3af9d952e918afb385342a9eae34"
)


class EmailTrendRendererContractTest(unittest.TestCase):
    def test_active_renderer_source_is_unchanged(self):
        source = inspect.getsource(build_trend_png).encode("utf-8")
        self.assertEqual(hashlib.sha256(source).hexdigest(), ACTIVE_RENDERER_SOURCE_SHA256)


if __name__ == "__main__":
    unittest.main()
