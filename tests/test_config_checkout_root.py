"""A worker checkout root inside a macOS privacy-protected folder is refused (#428)."""
import os
import tempfile
import unittest
from unittest import mock

from factory_mcp import config
from factory_mcp.config import Config

HOME = "/Users/tester"
ROOT = os.path.join(HOME, ".local", "share", "factory-work")


def load(root, platform="darwin", home=HOME):
    env = {"FACTORY_MCP_TOKEN": "t", "FACTORY_CHECKOUT_ROOT": root, "HOME": home}
    with mock.patch.object(config.sys, "platform", platform):
        return Config.from_env(env)


class CheckoutRootTest(unittest.TestCase):
    def test_darwin_refuses_a_root_inside_a_protected_folder(self):
        for folder in ("Documents", "Desktop", "Downloads",
                       "Library/Mobile Documents", "Library/CloudStorage"):
            with self.subTest(folder=folder):
                with self.assertRaisesRegex(ValueError, "is inside ~/" + folder):
                    load(os.path.join(HOME, folder, "factory-work"))

    def test_darwin_loads_a_root_outside_them(self):
        self.assertEqual(load(ROOT).checkout_root, ROOT)

    def test_linux_loads_a_root_inside_documents(self):
        self.assertEqual(load(os.path.join(HOME, "Documents", "factory-work"),
                              platform="linux").checkout_root,
                         os.path.join(HOME, "Documents", "factory-work"))

    def test_a_sibling_of_a_protected_folder_is_not_protected(self):
        sibling = os.path.join(HOME, "Documents-old", "factory-work")
        self.assertEqual(load(sibling).checkout_root, sibling)

    def test_case_does_not_hide_a_protected_folder(self):
        with self.assertRaisesRegex(ValueError, "is inside ~/Documents"):
            load(os.path.join(HOME, "documents", "factory-work"))

    def test_a_symlink_into_a_protected_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, "Documents", "factory-work"))
            link = os.path.join(home, "factory-work")
            os.symlink(os.path.join(home, "Documents", "factory-work"), link)
            with self.assertRaisesRegex(ValueError, "is inside ~/Documents"):
                load(link, home=home)
