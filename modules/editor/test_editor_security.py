import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import module


class EditorPathSecurityTests(unittest.TestCase):
    def test_rejects_prefix_collision_for_existing_path(self):
        with tempfile.TemporaryDirectory() as root:
            allowed = os.path.join(root, "safe")
            escaped = os.path.join(root, "safe_evil")
            os.makedirs(allowed)
            os.makedirs(escaped)
            target = os.path.join(escaped, "note.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("nope")

            resolved, err = module._resolve_path(target, {"allowed_paths": [allowed]})

            self.assertIsNone(resolved)
            self.assertIn("Pfad nicht erlaubt", err)

    def test_rejects_prefix_collision_for_new_path(self):
        with tempfile.TemporaryDirectory() as root:
            allowed = os.path.join(root, "safe")
            escaped = os.path.join(root, "safe_evil")
            os.makedirs(allowed)

            resolved, err = module._resolve_path(
                os.path.join(escaped, "new.txt"),
                {"allowed_paths": [allowed]},
            )

            self.assertIsNone(resolved)
            self.assertIn("Pfad nicht erlaubt", err)

    def test_allows_descendant_path(self):
        with tempfile.TemporaryDirectory() as root:
            allowed = os.path.join(root, "safe")
            nested = os.path.join(allowed, "nested")
            os.makedirs(nested)
            target = os.path.join(nested, "note.txt")

            resolved, err = module._resolve_path(target, {"allowed_paths": [allowed]})

            self.assertIsNone(err)
            self.assertEqual(os.path.realpath(target), resolved)

    def test_modules_alias_resolves_to_platform_modules_dir(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, "home", "chat.llamacpp")
            modules_dir = os.path.join(root, "modules")
            os.makedirs(os.path.join(modules_dir, "deepdive"))
            target = os.path.join(modules_dir, "deepdive", "module.py")

            resolved, err = module._resolve_path(
                "modules/deepdive/module.py",
                {"home_dir": home, "modules_dir": modules_dir},
            )

            self.assertIsNone(err)
            self.assertEqual(os.path.realpath(target), resolved)

    def test_existing_module_relative_path_resolves_to_modules_dir(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, "home", "chat.llamacpp")
            modules_dir = os.path.join(root, "modules")
            os.makedirs(os.path.join(modules_dir, "deepdive"))
            target = os.path.join(modules_dir, "deepdive", "module.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x")

            resolved, err = module._resolve_path(
                "deepdive/module.py",
                {"home_dir": home, "modules_dir": modules_dir},
            )

            self.assertIsNone(err)
            self.assertEqual(os.path.realpath(target), resolved)

    def test_named_path_prefix_is_stripped(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, "home", "chat.llamacpp")
            modules_dir = os.path.join(root, "modules")
            os.makedirs(os.path.join(modules_dir, "deepdive"))
            target = os.path.join(modules_dir, "deepdive", "module.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("x")

            resolved, err = module._resolve_path(
                "pfad: modules/deepdive/module.py",
                {"home_dir": home, "modules_dir": modules_dir},
            )

            self.assertIsNone(err)
            self.assertEqual(os.path.realpath(target), resolved)

    def test_overwrite_updates_existing_file_with_backup(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, "home", "chat.llamacpp")
            modules_dir = os.path.join(root, "modules")
            os.makedirs(os.path.join(modules_dir, "deepdive"))
            target = os.path.join(modules_dir, "deepdive", "module.py")
            with open(target, "w", encoding="utf-8") as f:
                f.write("old")

            result = module.handle_tool(
                "editor.overwrite",
                ["pfad: modules/deepdive/module.py", 'inhalt="""\nnew\n"""'],
                {"home_dir": home, "modules_dir": modules_dir},
            )

            self.assertTrue(result["success"], result)
            with open(target, encoding="utf-8") as f:
                self.assertEqual("new\n", f.read())
            self.assertTrue(os.path.exists(os.path.join(modules_dir, "deepdive", ".module.py.bak.1")))

    def test_modules_alias_cannot_escape_modules_dir(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, "home", "chat.llamacpp")
            modules_dir = os.path.join(root, "modules")
            project_root = root
            os.makedirs(modules_dir)

            resolved, err = module._resolve_path(
                "modules/../config.json",
                {"home_dir": home, "modules_dir": modules_dir, "project_root": project_root},
            )

            self.assertIsNone(resolved)
            self.assertIn("Pfad nicht erlaubt", err)

    def test_absolute_project_root_path_is_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, "agent-data", "home", "chat.llamacpp")
            modules_dir = os.path.join(root, "modules")
            os.makedirs(home)
            os.makedirs(modules_dir)
            target = os.path.join(root, "Cargo.toml")
            with open(target, "w", encoding="utf-8") as f:
                f.write("[package]\n")

            resolved, err = module._resolve_path(
                target,
                {"home_dir": home, "modules_dir": modules_dir, "project_root": root},
            )

            self.assertIsNone(err)
            self.assertEqual(os.path.realpath(target), resolved)

    def test_relative_project_root_path_resolves_when_parent_exists(self):
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, "agent-data", "home", "chat.llamacpp")
            modules_dir = os.path.join(root, "modules")
            src_dir = os.path.join(root, "src")
            os.makedirs(home)
            os.makedirs(modules_dir)
            os.makedirs(src_dir)
            target = os.path.join(src_dir, "main.rs")
            with open(target, "w", encoding="utf-8") as f:
                f.write("fn main() {}\n")

            resolved, err = module._resolve_path(
                "src/main.rs",
                {"home_dir": home, "modules_dir": modules_dir, "project_root": root},
            )

            self.assertIsNone(err)
            self.assertEqual(os.path.realpath(target), resolved)

    def test_equivalent_symlink_and_real_path_are_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            real_root = os.path.join(root, "real")
            alias_root = os.path.join(root, "alias")
            os.makedirs(real_root)
            os.symlink(real_root, alias_root)
            target = os.path.join(real_root, "note.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("ok")

            resolved, err = module._resolve_path(
                target,
                {"allowed_paths": [alias_root]},
            )

            self.assertIsNone(err)
            self.assertEqual(os.path.realpath(target), resolved)

    def test_allowed_paths_accepts_single_string(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "note.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("ok")

            resolved, err = module._resolve_path(
                target,
                {"allowed_paths": root},
            )

            self.assertIsNone(err)
            self.assertEqual(os.path.realpath(target), resolved)


if __name__ == "__main__":
    unittest.main()
