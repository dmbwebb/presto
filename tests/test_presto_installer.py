#!/usr/bin/env python3
import unittest
from pathlib import Path


INSTALL_SH = Path(__file__).resolve().parents[1] / "presto" / "app" / "install.sh"
GENERATE_ICON_SWIFT = Path(__file__).resolve().parents[1] / "presto" / "app" / "generate_icon.swift"


class PrestoInstallerTests(unittest.TestCase):
    def test_installer_uses_recoverable_trash_instead_of_rm_rf(self):
        script = INSTALL_SH.read_text(encoding="utf-8")
        self.assertNotIn("rm -rf", script)
        self.assertIn("trash", script)
        self.assertIn("Existing /Applications/Presto.app", script)

    def test_installer_compiles_binary_to_temp_before_publishing(self):
        script = INSTALL_SH.read_text(encoding="utf-8")

        self.assertIn("APP_BINARY=", script)
        self.assertIn("BUILD_DIR=", script)
        self.assertIn("TMP_BINARY=", script)
        self.assertIn('TMP_BINARY="$BUILD_DIR/Presto"', script)
        self.assertIn('swiftc -framework WebKit -framework Cocoa -framework PDFKit', script)
        self.assertIn('"$PROJECT_DIR/presto/RefineSubprocess.swift"', script)
        self.assertIn('"$PROJECT_DIR/presto/PDFSnapshot.swift"', script)
        self.assertIn('"$PROJECT_DIR/presto/Presto.swift"', script)
        for name in ("DeckLibrary", "PrestoWindow", "PrestoPresent", "PrestoInjection", "PrestoMenus"):
            self.assertIn(f'"$PROJECT_DIR/presto/{name}.swift"', script)
        # Every app source under presto/ is compiled into the binary.
        for swift in sorted(INSTALL_SH.parents[1].glob("*.swift")):
            self.assertIn(f'"$PROJECT_DIR/presto/{swift.name}"', script, swift.name)
        self.assertNotIn("-framework Quartz", script)
        self.assertIn('-o "$TMP_BINARY"', script)
        self.assertIn('mv "$TMP_BINARY" "$APP_BINARY"', script)
        self.assertNotIn('-o "$SCRIPT_DIR/Presto.app/Contents/MacOS/Presto"', script)

    def test_installer_signs_and_strictly_verifies_source_and_installed_bundles(self):
        script = INSTALL_SH.read_text(encoding="utf-8")

        self.assertIn('codesign --force --deep --sign - "$APP_BUNDLE"', script)
        self.assertIn('codesign --verify --deep --strict "$APP_BUNDLE"', script)
        self.assertIn('codesign --verify --deep --strict "/Applications/Presto.app"', script)

    def test_installer_publishes_copy_instead_of_source_symlink(self):
        script = INSTALL_SH.read_text(encoding="utf-8")

        self.assertIn('ditto "$APP_BUNDLE" "$INSTALL_STAGING"', script)
        self.assertIn('mv "$INSTALL_STAGING" "/Applications/Presto.app"', script)
        self.assertNotIn('ln -s "$SCRIPT_DIR/Presto.app"', script)

    def test_installer_binary_target_matches_declared_minimum_macos(self):
        script = INSTALL_SH.read_text(encoding="utf-8")

        self.assertIn('-target "${TARGET_ARCH}-apple-macos13.0"', script)

    def test_icon_is_light_with_the_accent_used_once(self):
        source = GENERATE_ICON_SWIFT.read_text(encoding="utf-8")
        self.assertNotIn("0.1, green: 0.1, blue: 0.12", source)
        self.assertNotIn("emoji", source)
        self.assertIn("color(0xFFFFFF)", source)
        self.assertEqual(source.count("color(0x2F6BD8)"), 1)

    def test_app_declares_folder_and_html_documents_and_light_appearance(self):
        import plistlib
        plist_path = INSTALL_SH.parent / "Presto.app" / "Contents" / "Info.plist"
        info = plistlib.loads(plist_path.read_bytes())
        types = {t for doc in info["CFBundleDocumentTypes"] for t in doc["LSItemContentTypes"]}
        self.assertEqual(types, {"public.html", "public.folder"})
        self.assertTrue(all(doc["LSHandlerRank"] == "Alternate" for doc in info["CFBundleDocumentTypes"]))
        self.assertTrue(info["NSRequiresAquaSystemAppearance"])

    def test_icon_generator_exits_nonzero_when_iconutil_fails(self):
        source = GENERATE_ICON_SWIFT.read_text(encoding="utf-8")

        self.assertIn("iconutil failed", source)
        self.assertIn("exit(1)", source)

    def test_icon_generator_reports_setup_and_launch_errors_cleanly(self):
        source = GENERATE_ICON_SWIFT.read_text(encoding="utf-8")

        self.assertIn('fail("Failed to create iconset directory:', source)
        self.assertIn('fail("Failed to render icon PNG', source)
        self.assertIn('fail("Failed to write icon PNG', source)
        self.assertIn('fail("Failed to launch iconutil:', source)
        self.assertNotIn('print("Failed to render', source)

        create_dir = source.index("FileManager.default.createDirectory")
        self.assertLess(create_dir, source.index("Failed to create iconset directory", create_dir))

        write_png = source.index("writeDataAtomically(pngData")
        self.assertLess(write_png, source.index("Failed to write icon PNG", write_png))

        iconutil_run = source.rindex("process.run()")
        self.assertLess(iconutil_run, source.index("Failed to launch iconutil", iconutil_run))

    def test_icon_generator_uses_recoverable_trash_cleanup(self):
        source = GENERATE_ICON_SWIFT.read_text(encoding="utf-8")

        self.assertIn("func trashPath", source)
        self.assertIn("/usr/bin/trash", source)
        self.assertIn("process.arguments = [url.path]", source)
        self.assertNotIn("/usr/bin/osascript", source)
        self.assertNotIn("move POSIX file", source)
        self.assertNotIn("FileManager.default.removeItem", source)

    def test_icon_generator_publishes_png_and_icns_outputs_atomically(self):
        source = GENERATE_ICON_SWIFT.read_text(encoding="utf-8")

        self.assertIn("func writeDataAtomically", source)
        self.assertIn("func publishFileAtomically", source)
        self.assertNotIn("pngData.write(to: outPath)", source)
        self.assertIn("writeDataAtomically(pngData, to: outPath)", source)
        self.assertIn("replaceItemAt(finalURL", source)
        self.assertIn("moveItem(at: tempURL, to: finalURL)", source)
        self.assertIn('"-o", tempIcnsPath.path', source)
        self.assertIn("publishFileAtomically(tempURL: tempIcnsPath, finalURL: icnsPath)", source)

    def test_icon_generator_keeps_temp_icns_extension_for_iconutil(self):
        source = GENERATE_ICON_SWIFT.read_text(encoding="utf-8")

        self.assertIn("func temporaryIconSibling", source)
        self.assertIn('tmp-\\(UUID().uuidString).icns"', source)
        self.assertIn("let tempIcnsPath = temporaryIconSibling(for: icnsPath)", source)
        self.assertNotIn("let tempIcnsPath = temporarySibling(for: icnsPath)", source)


if __name__ == "__main__":
    unittest.main()
