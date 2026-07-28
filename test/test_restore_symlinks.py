
# Regression test: browser profile restore must survive Chromium's broken
# Singleton{Lock,Cookie,Socket} symlinks. These point at a dead PID, so
# shutil.copytree WITHOUT symlinks=True follows them and crashes with ENOENT,
# which used to 500 the /api/settings/browser/restore endpoint AFTER it had
# already deleted the live profile. Keep symlinks=True.
#
# Run: python3 test/test_restore_symlinks.py
import shutil
import tempfile
from pathlib import Path


def _make_fake_profile(root: Path) -> Path:
    """Mimic a Chromium profile dir, including broken Singleton symlinks."""
    prof = root / "profile"
    (prof / "Default").mkdir(parents=True)
    (prof / "Default" / "Cookies").write_text("fake-cookie-db")
    (prof / "Default" / "Local State").write_text("{}")
    # Broken symlinks exactly like Chromium creates (target PID is dead)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (prof / name).symlink_to("/proc/999999999/fd/42")
    return prof


def test_default_copytree_fails_on_broken_symlinks() -> None:
    """Documents the original bug: default copytree raises on broken symlinks."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = _make_fake_profile(root)
        dst = root / "restored"
        try:
            shutil.copytree(src, dst)  # follows symlinks -> ENOENT
            raised = False
        except shutil.Error:
            raised = True
        assert raised, "expected default copytree to fail on broken symlinks"
        print("PASS: default copytree fails on broken symlinks (bug reproduced)")


def test_symlinks_true_restore_succeeds() -> None:
    """The fix: symlinks=True copies broken symlinks as-is, no crash."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = _make_fake_profile(root)
        dst = root / "restored"
        shutil.copytree(src, dst, symlinks=True)  # must not raise
        assert (dst / "Default" / "Cookies").read_text() == "fake-cookie-db"
        assert (dst / "SingletonLock").is_symlink()
        print("PASS: symlinks=True restore succeeds, data + symlinks intact")


if __name__ == "__main__":
    test_default_copytree_fails_on_broken_symlinks()
    test_symlinks_true_restore_succeeds()
    print("\nAll restore-symlink regression tests passed ✅")
