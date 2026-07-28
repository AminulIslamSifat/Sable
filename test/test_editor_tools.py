
#!/usr/bin/env python3
"""
Comprehensive test suite for editor_tools.py v4.
Tests all matching layers, mutation ops, edge cases, and error paths.
Run: cd /home/sifat/hdd/projects/Sable && uv run python -m pytest test/test_editor_tools.py -v
"""

import os
import stat
import sys
import tempfile
import shutil
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "core", "code_editor", "scripts"))
import editor_tools as et


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix="editor_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def sample_file(tmp_dir):
    path = os.path.join(tmp_dir, "sample.py")
    content = (
        "def hello(name):\n"
        "    print(f'Hello, {name}!')\n"
        "\n"
        "\n"
        "def goodbye(name):\n"
        "    print(f'Bye, {name}!')\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    hello('world')\n"
    )
    with open(path, "w") as f:
        f.write(content)
    return path


@pytest.fixture
def crlf_file(tmp_dir):
    path = os.path.join(tmp_dir, "crlf.txt")
    content = "line one\r\nline two\r\nline three\r\n"
    with open(path, "wb") as f:
        f.write(content.encode("utf-8"))
    return path


@pytest.fixture
def tab_file(tmp_dir):
    path = os.path.join(tmp_dir, "tabs.py")
    content = "def foo():\n\tprint('tabbed')\n\treturn True\n"
    with open(path, "w") as f:
        f.write(content)
    return path


# ==========================================================================
# view_file tests
# ==========================================================================

class TestViewFile:
    def test_view_full(self, sample_file):
        result = et.view_file(sample_file, full=True)
        assert "def hello(name):" in result
        assert "def goodbye(name):" in result
        # Line numbers present
        assert "1\t" in result

    def test_view_range(self, sample_file):
        result = et.view_file(sample_file, start=1, end=2)
        assert "def hello(name):" in result
        assert "def goodbye" not in result

    def test_view_auto_truncation(self, tmp_dir):
        path = os.path.join(tmp_dir, "big.py")
        # Need >16000 chars to trigger truncation; use long lines
        lines = [f"line_{i} = '{'x' * 40}_{i}'" for i in range(500)]
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
        result = et.view_file(path)
        assert "omitted" in result
        assert "500 lines total" in result

    def test_view_directory(self, tmp_dir):
        os.makedirs(os.path.join(tmp_dir, "subdir"))
        with open(os.path.join(tmp_dir, "file.txt"), "w") as f:
            f.write("x")
        result = et.view_file(tmp_dir)
        assert "subdir/" in result
        assert "file.txt" in result

    def test_view_empty_file(self, tmp_dir):
        path = os.path.join(tmp_dir, "empty.py")
        with open(path, "w") as f:
            f.write("")
        result = et.view_file(path)
        assert result == "(empty file)"

    def test_view_nonexistent(self, tmp_dir):
        with pytest.raises(et.ToolError, match="does not exist"):
            et.view_file(os.path.join(tmp_dir, "nope.py"))

    def test_view_start_beyond_end(self, sample_file):
        with pytest.raises(et.ToolError, match="beyond end"):
            et.view_file(sample_file, start=999)


# ==========================================================================
# create_file tests
# ==========================================================================

class TestCreateFile:
    def test_create_new(self, tmp_dir):
        path = os.path.join(tmp_dir, "new.py")
        result = et.create_file(path, "print('hi')\n")
        assert "Created" in result
        assert os.path.isfile(path)
        with open(path) as f:
            assert f.read() == "print('hi')\n"

    def test_create_nested_dirs(self, tmp_dir):
        path = os.path.join(tmp_dir, "a", "b", "c", "deep.py")
        et.create_file(path, "x = 1\n")
        assert os.path.isfile(path)

    def test_create_existing_fails(self, sample_file):
        with pytest.raises(et.ToolError, match="already exists"):
            et.create_file(sample_file, "overwrite attempt")

    def test_create_overwrite(self, sample_file):
        result = et.create_file(sample_file, "replaced\n", overwrite=True)
        assert "Created" in result
        with open(sample_file) as f:
            assert f.read() == "replaced\n"

    def test_create_shebang_chmod(self, tmp_dir):
        path = os.path.join(tmp_dir, "script.sh")
        et.create_file(path, "#!/bin/bash\necho hi\n")
        st = os.stat(path)
        assert st.st_mode & stat.S_IXUSR

    def test_create_no_shebang_no_exec(self, tmp_dir):
        path = os.path.join(tmp_dir, "plain.py")
        et.create_file(path, "x = 1\n")
        st = os.stat(path)
        assert not (st.st_mode & stat.S_IXUSR)

    def test_create_stats_output(self, tmp_dir):
        path = os.path.join(tmp_dir, "stats.py")
        result = et.create_file(path, "a\nb\nc\n")
        assert "── stats ──" in result
        assert "lines: 3" in result
        assert "bytes:" in result


# ==========================================================================
# edit_file — Layer 1: exact matching
# ==========================================================================

class TestEditExact:
    def test_simple_replace(self, sample_file):
        result = et.edit_file(sample_file, {
            "old_str": "def hello(name):",
            "new_str": "def greet(name):",
        })
        assert "Edited" in result
        with open(sample_file) as f:
            assert "def greet(name):" in f.read()

    def test_multiline_replace(self, sample_file):
        result = et.edit_file(sample_file, {
            "old_str": "def hello(name):\n    print(f'Hello, {name}!')",
            "new_str": "def hello(name: str) -> None:\n    print(f'Hey, {name}!')",
        })
        assert "Edited" in result
        with open(sample_file) as f:
            content = f.read()
        assert "def hello(name: str) -> None:" in content
        assert "Hey," in content

    def test_delete_lines(self, sample_file):
        result = et.edit_file(sample_file, {
            "old_str": "    hello('world')\n",
            "new_str": "",
        })
        assert "Edited" in result
        with open(sample_file) as f:
            assert "hello('world')" not in f.read()

    def test_ambiguous_match_fails(self, sample_file):
        with pytest.raises(et.ToolError, match="matches 2 locations"):
            et.edit_file(sample_file, {
                "old_str": "    print(f'",
                "new_str": "    log(f'",
            })

    def test_no_match_fails(self, sample_file):
        with pytest.raises(et.ToolError, match="not found"):
            et.edit_file(sample_file, {
                "old_str": "this does not exist in the file",
                "new_str": "replacement",
            })

    def test_empty_old_str_fails(self, sample_file):
        with pytest.raises(et.ToolError, match="empty"):
            et.edit_file(sample_file, {"old_str": "", "new_str": "x"})

    def test_identical_old_new_fails(self, sample_file):
        with pytest.raises(et.ToolError, match="identical"):
            et.edit_file(sample_file, {
                "old_str": "def hello(name):",
                "new_str": "def hello(name):",
            })

    def test_nonexistent_file(self, tmp_dir):
        with pytest.raises(et.ToolError, match="does not exist"):
            et.edit_file(os.path.join(tmp_dir, "nope.py"), {
                "old_str": "x", "new_str": "y"
            })


# ==========================================================================
# edit_file — Layer 2: normalized matching
# ==========================================================================

class TestEditNormalized:
    def test_smart_quotes(self, tmp_dir):
        path = os.path.join(tmp_dir, "quotes.py")
        with open(path, "w") as f:
            f.write("msg = \u201chello world\u201d\n")
        # Model types straight quotes
        result = et.edit_file(path, {
            "old_str": 'msg = "hello world"',
            "new_str": 'msg = "goodbye"',
        })
        assert "Edited" in result
        assert "normaliz" in result  # note about normalization

    def test_unicode_dash(self, tmp_dir):
        path = os.path.join(tmp_dir, "dash.py")
        with open(path, "w") as f:
            f.write("x = 5 \u2014 3\n")  # em dash
        result = et.edit_file(path, {
            "old_str": "x = 5 - 3",
            "new_str": "x = 2",
        })
        assert "Edited" in result

    def test_trailing_whitespace(self, tmp_dir):
        path = os.path.join(tmp_dir, "trailing.py")
        with open(path, "w") as f:
            f.write("x = 1   \ny = 2  \n")
        result = et.edit_file(path, {
            "old_str": "x = 1\ny = 2",
            "new_str": "x = 10\ny = 20",
        })
        assert "Edited" in result

    def test_tab_vs_spaces_normalized(self, tab_file):
        # File has tabs, model sends 4-space indent
        result = et.edit_file(tab_file, {
            "old_str": "def foo():\n    print('tabbed')\n    return True",
            "new_str": "def foo():\n    print('spaced')\n    return False",
        })
        assert "Edited" in result
        with open(tab_file) as f:
            content = f.read()
        assert "spaced" in content


# ==========================================================================
# edit_file — Layer 3: structural matching
# ==========================================================================

class TestEditStructural:
    def test_indent_mismatch(self, tmp_dir):
        path = os.path.join(tmp_dir, "indent.py")
        with open(path, "w") as f:
            f.write("def foo():\n        print('deep indent')\n        return 1\n")
        # Model sends different indentation
        result = et.edit_file(path, {
            "old_str": "def foo():\n  print('deep indent')\n  return 1",
            "new_str": "def foo():\n    print('fixed')\n    return 2",
        })
        assert "Edited" in result
        assert "structural" in result

    def test_extra_blank_lines(self, tmp_dir):
        path = os.path.join(tmp_dir, "blanks.py")
        with open(path, "w") as f:
            f.write("x = 1\n\n\n\ny = 2\n")
        # Model sends single blank line
        result = et.edit_file(path, {
            "old_str": "x = 1\n\ny = 2",
            "new_str": "x = 10\n\ny = 20",
        })
        assert "Edited" in result

    def test_leading_trailing_blanks_in_old_str(self, tmp_dir):
        path = os.path.join(tmp_dir, "edge_blanks.py")
        with open(path, "w") as f:
            f.write("aaa\nbbb\nccc\n")
        # Model adds extra blank at start/end of old_str
        result = et.edit_file(path, {
            "old_str": "\naaa\nbbb\nccc\n",
            "new_str": "xxx\nyyy\nzzz",
        })
        assert "Edited" in result

    def test_structural_ambiguous_fails(self, tmp_dir):
        path = os.path.join(tmp_dir, "dup.py")
        with open(path, "w") as f:
            f.write("    x = 1\n    y = 2\n\n    x = 1\n    y = 2\n")
        with pytest.raises(et.ToolError, match="matches 2 locations"):
            et.edit_file(path, {
                "old_str": "x = 1\ny = 2",
                "new_str": "x = 99\ny = 99",
            })


# ==========================================================================
# edit_file — batch (atomic multi-edit)
# ==========================================================================

class TestEditBatch:
    def test_two_edits(self, sample_file):
        result = et.edit_file(sample_file, [
            {"old_str": "def hello(name):", "new_str": "def hi(name):"},
            {"old_str": "def goodbye(name):", "new_str": "def bye(name):"},
        ])
        assert "2 changes" in result
        with open(sample_file) as f:
            content = f.read()
        assert "def hi(name):" in content
        assert "def bye(name):" in content

    def test_overlapping_edits_fail(self, tmp_dir):
        path = os.path.join(tmp_dir, "overlap.py")
        with open(path, "w") as f:
            f.write("aaa bbb ccc\n")
        with pytest.raises(et.ToolError, match="overlap"):
            et.edit_file(path, [
                {"old_str": "aaa bbb", "new_str": "xxx"},
                {"old_str": "bbb ccc", "new_str": "yyy"},
            ])

    def test_second_edit_fails_whole_batch(self, sample_file):
        with pytest.raises(et.ToolError, match="edit #2"):
            et.edit_file(sample_file, [
                {"old_str": "def hello(name):", "new_str": "def hi(name):"},
                {"old_str": "NONEXISTENT", "new_str": "whatever"},
            ])
        # File should be unchanged (atomic)
        with open(sample_file) as f:
            assert "def hello(name):" in f.read()


# ==========================================================================
# edit_file — replace_all
# ==========================================================================

class TestEditReplaceAll:
    def test_replace_all_exact(self, tmp_dir):
        path = os.path.join(tmp_dir, "multi.py")
        with open(path, "w") as f:
            f.write("old_name = 1\nprint(old_name)\nold_name = 2\n")
        result = et.edit_file(path, {
            "old_str": "old_name",
            "new_str": "new_name",
        }, replace_all=True)
        assert "3 replacements" in result
        with open(path) as f:
            content = f.read()
        assert "old_name" not in content
        assert content.count("new_name") == 3

    def test_replace_all_not_found(self, tmp_dir):
        path = os.path.join(tmp_dir, "empty.py")
        with open(path, "w") as f:
            f.write("x = 1\n")
        with pytest.raises(et.ToolError, match="not found"):
            et.edit_file(path, {
                "old_str": "zzz",
                "new_str": "yyy",
            }, replace_all=True)


# ==========================================================================
# edit_file — dry_run
# ==========================================================================

class TestEditDryRun:
    def test_dry_run_no_write(self, sample_file):
        with open(sample_file) as f:
            original = f.read()
        result = et.edit_file(sample_file, {
            "old_str": "def hello(name):",
            "new_str": "def DRY(name):",
        }, dry_run=True)
        assert "DRY RUN" in result
        assert "NO changes written" in result
        with open(sample_file) as f:
            assert f.read() == original

    def test_dry_run_shows_diff(self, sample_file):
        result = et.edit_file(sample_file, {
            "old_str": "def hello(name):",
            "new_str": "def greet(name):",
        }, dry_run=True)
        assert "-def hello(name):" in result
        assert "+def greet(name):" in result


# ==========================================================================
# edit_file — stats output
# ==========================================================================

class TestEditStats:
    def test_stats_present(self, sample_file):
        result = et.edit_file(sample_file, {
            "old_str": "def hello(name):",
            "new_str": "def greet(name):\n    # new line",
        })
        assert "── stats ──" in result
        assert "lines_before:" in result
        assert "lines_after:" in result
        assert "added:" in result
        assert "removed:" in result
        assert "net:" in result
        assert "affected_range:" in result


# ==========================================================================
# edit_file — nearest match suggestion
# ==========================================================================

class TestNearestMatch:
    def test_suggestion_on_typo(self, sample_file):
        with pytest.raises(et.ToolError) as exc_info:
            et.edit_file(sample_file, {
                "old_str": "def hello(nam):",  # typo
                "new_str": "def greet(name):",
            })
        err = str(exc_info.value)
        assert "Closest match" in err
        assert "ratio" in err


# ==========================================================================
# insert_file tests
# ==========================================================================

class TestInsertFile:
    def test_insert_at_line(self, sample_file):
        result = et.insert_file(sample_file, "# inserted comment\n", at_line=1)
        assert "Inserted" in result
        with open(sample_file) as f:
            first_line = f.readline()
        assert first_line == "# inserted comment\n"

    def test_insert_after_str(self, sample_file):
        result = et.insert_file(sample_file, "    # after hello\n", after_str="def hello(name):")
        assert "Inserted" in result
        with open(sample_file) as f:
            content = f.read()
        assert "def hello(name):\n    # after hello\n" in content

    def test_insert_at_line_out_of_range(self, sample_file):
        with pytest.raises(et.ToolError, match="out of range"):
            et.insert_file(sample_file, "x\n", at_line=999)

    def test_insert_requires_one_anchor(self, sample_file):
        with pytest.raises(et.ToolError, match="exactly one"):
            et.insert_file(sample_file, "x\n")

    def test_insert_both_anchors_fails(self, sample_file):
        with pytest.raises(et.ToolError, match="exactly one"):
            et.insert_file(sample_file, "x\n", at_line=1, after_str="def")

    def test_insert_dry_run(self, sample_file):
        with open(sample_file) as f:
            original = f.read()
        result = et.insert_file(sample_file, "new line\n", at_line=1, dry_run=True)
        assert "DRY RUN" in result
        with open(sample_file) as f:
            assert f.read() == original

    def test_insert_stats(self, sample_file):
        result = et.insert_file(sample_file, "a\nb\nc", at_line=1)
        assert "── stats ──" in result
        assert "3 lines added" in result


# ==========================================================================
# CRLF preservation
# ==========================================================================

class TestCRLF:
    def test_crlf_preserved(self, crlf_file):
        et.edit_file(crlf_file, {
            "old_str": "line two",
            "new_str": "line TWO",
        })
        with open(crlf_file, "rb") as f:
            raw = f.read()
        assert b"\r\n" in raw
        assert b"line TWO" in raw

    def test_lf_stays_lf(self, sample_file):
        et.edit_file(sample_file, {
            "old_str": "def hello(name):",
            "new_str": "def hi(name):",
        })
        with open(sample_file, "rb") as f:
            raw = f.read()
        assert b"\r\n" not in raw


# ==========================================================================
# Encoding guard
# ==========================================================================

class TestEncoding:
    def test_binary_file_rejected(self, tmp_dir):
        path = os.path.join(tmp_dir, "binary.bin")
        with open(path, "wb") as f:
            f.write(b"\x00\x01\x02\xff\xfe")
        with pytest.raises(et.ToolError, match="binary|not UTF-8"):
            et.view_file(path)

    def test_latin1_rejected(self, tmp_dir):
        path = os.path.join(tmp_dir, "latin.txt")
        with open(path, "wb") as f:
            f.write("café".encode("latin-1"))
        with pytest.raises(et.ToolError, match="not UTF-8|binary"):
            et._read_raw(path)


# ==========================================================================
# Backup system
# ==========================================================================

class TestBackup:
    def test_backup_created_on_edit(self, sample_file):
        et.edit_file(sample_file, {
            "old_str": "def hello(name):",
            "new_str": "def hi(name):",
        })
        backup_dir = os.path.join(os.path.dirname(sample_file), ".editor_tools_backups")
        assert os.path.isdir(backup_dir)
        backups = [f for f in os.listdir(backup_dir) if "sample.py" in f]
        assert len(backups) >= 1

    def test_backup_rotation(self, tmp_dir):
        path = os.path.join(tmp_dir, "rot.py")
        with open(path, "w") as f:
            f.write("v0\n")
        # Create 25 backups
        for i in range(25):
            with open(path, "w") as f:
                f.write(f"v{i}\n")
            et._backup(path)
        backup_dir = os.path.join(tmp_dir, ".editor_tools_backups")
        backups = [f for f in os.listdir(backup_dir) if "rot.py" in f]
        assert len(backups) <= et.MAX_BACKUPS_PER_FILE


# ==========================================================================
# SEARCH/REPLACE parser
# ==========================================================================

class TestParser:
    def test_single_block(self):
        text = "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE"
        result = et._parse_search_replace_blocks(text)
        assert result == {"old_str": "old", "new_str": "new"}

    def test_multiple_blocks(self):
        text = (
            "<<<<<<< SEARCH\naaa\n=======\nbbb\n>>>>>>> REPLACE\n"
            "\n"
            "<<<<<<< SEARCH\nccc\n=======\nddd\n>>>>>>> REPLACE\n"
        )
        result = et._parse_search_replace_blocks(text)
        assert len(result) == 2
        assert result[0] == {"old_str": "aaa", "new_str": "bbb"}
        assert result[1] == {"old_str": "ccc", "new_str": "ddd"}

    def test_empty_replace(self):
        text = "<<<<<<< SEARCH\ndelete me\n=======\n>>>>>>> REPLACE"
        result = et._parse_search_replace_blocks(text)
        assert result["new_str"] == ""

    def test_unterminated_fails(self):
        with pytest.raises(et.ToolError, match="Unterminated"):
            et._parse_search_replace_blocks("<<<<<<< SEARCH\nold\n=======\nnew")

    def test_no_blocks_fails(self):
        with pytest.raises(et.ToolError, match="No SEARCH/REPLACE"):
            et._parse_search_replace_blocks("just plain text")

    def test_multiline_content(self):
        text = "<<<<<<< SEARCH\ndef foo():\n    return 1\n=======\ndef bar():\n    return 2\n>>>>>>> REPLACE"
        result = et._parse_search_replace_blocks(text)
        assert "def foo():" in result["old_str"]
        assert "def bar():" in result["new_str"]


# ==========================================================================
# Edge cases & integration
# ==========================================================================

class TestEdgeCases:
    def test_edit_last_line_no_trailing_newline(self, tmp_dir):
        path = os.path.join(tmp_dir, "no_newline.py")
        with open(path, "w") as f:
            f.write("x = 1\ny = 2")  # no trailing \n
        et.edit_file(path, {"old_str": "y = 2", "new_str": "y = 99"})
        with open(path) as f:
            content = f.read()
        assert content == "x = 1\ny = 99"

    def test_edit_preserves_trailing_newline(self, tmp_dir):
        path = os.path.join(tmp_dir, "with_newline.py")
        with open(path, "w") as f:
            f.write("x = 1\ny = 2\n")
        et.edit_file(path, {"old_str": "y = 2\n", "new_str": "y = 99\n"})
        with open(path) as f:
            content = f.read()
        assert content.endswith("\n")

    def test_replace_with_empty_string_content(self, tmp_dir):
        path = os.path.join(tmp_dir, "del.py")
        with open(path, "w") as f:
            f.write("keep\nremove me\nkeep2\n")
        et.edit_file(path, {"old_str": "remove me\n", "new_str": ""})
        with open(path) as f:
            content = f.read()
        assert content == "keep\nkeep2\n"

    def test_special_chars_in_content(self, tmp_dir):
        path = os.path.join(tmp_dir, "special.py")
        content = 'x = "${HOME}/path"\ny = f"{val!r}"\n'
        with open(path, "w") as f:
            f.write(content)
        et.edit_file(path, {
            "old_str": 'x = "${HOME}/path"',
            "new_str": 'x = os.path.expanduser("~")',
        })
        with open(path) as f:
            assert "expanduser" in f.read()

    def test_unicode_content(self, tmp_dir):
        path = os.path.join(tmp_dir, "unicode.py")
        with open(path, "w") as f:
            f.write("greeting = 'こんにちは'\n")
        et.edit_file(path, {
            "old_str": "greeting = 'こんにちは'",
            "new_str": "greeting = '안녕하세요'",
        })
        with open(path) as f:
            assert "안녕하세요" in f.read()

    def test_very_long_line(self, tmp_dir):
        path = os.path.join(tmp_dir, "long.py")
        long_str = "x" * 5000
        with open(path, "w") as f:
            f.write(f"val = '{long_str}'\n")
        et.edit_file(path, {
            "old_str": f"val = '{long_str}'",
            "new_str": "val = 'short'",
        })
        with open(path) as f:
            assert "short" in f.read()

    def test_insert_at_end_of_file(self, sample_file):
        with open(sample_file) as f:
            total = len(f.readlines())
        et.insert_file(sample_file, "# end marker", at_line=total + 1)
        with open(sample_file) as f:
            content = f.read()
        assert "# end marker" in content
        # Should be near the end
        assert content.strip().endswith("# end marker")

    def test_nbsp_normalized(self, tmp_dir):
        path = os.path.join(tmp_dir, "nbsp.py")
        with open(path, "w") as f:
            f.write("x\u00a0=\u00a01\n")  # non-breaking spaces
        result = et.edit_file(path, {
            "old_str": "x = 1",
            "new_str": "x = 2",
        })
        assert "Edited" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])



# ==========================================================================
# Bug-hunting: structural replace_all span calculation
# ==========================================================================

class TestStructuralReplaceAll:
    """Tests targeting the _find_all_spans structural fallback.
    The bug: when structural matching collapses blank lines, the span
    calculated as len(old_lines) may be too short, truncating the replacement."""

    def test_replace_all_structural_with_extra_blanks(self, tmp_dir):
        """File has extra blank lines between repeated blocks.
        replace_all with structural matching should replace the FULL span
        including the extra blanks — y = 2 must be consumed too."""
        path = os.path.join(tmp_dir, "blanks_dup.py")
        with open(path, "w") as f:
            f.write("x = 1\n\n\n\ny = 2\n\nsome other stuff\n\nx = 1\n\n\n\ny = 2\n")
        result = et.edit_file(path, {
            "old_str": "x = 1\n\ny = 2",
            "new_str": "REPLACED",
        }, replace_all=True)
        assert "Edited" in result
        with open(path) as f:
            content = f.read()
        assert content.count("REPLACED") == 2
        assert "x = 1" not in content
        # BUG CHECK: y = 2 was part of the matched block and must be gone
        assert "y = 2" not in content, (
            f"Structural span too short — 'y = 2' survived. Got: {content!r}"
        )

    def test_replace_all_structural_tabs(self, tmp_dir):
        """File uses tabs, old_str uses spaces, replace_all should get all."""
        path = os.path.join(tmp_dir, "tab_dup.py")
        with open(path, "w") as f:
            f.write("\tprint('hello')\n\tprint('world')\n\n\tprint('hello')\n\tprint('world')\n")
        result = et.edit_file(path, {
            "old_str": "    print('hello')\n    print('world')",
            "new_str": "    print('REPLACED')",
        }, replace_all=True)
        assert "Edited" in result
        with open(path) as f:
            content = f.read()
        assert content.count("REPLACED") == 2
        assert "hello" not in content

    def test_structural_span_includes_all_file_lines(self, tmp_dir):
        """Verify the replaced span covers the correct number of file lines.
        File has extra blanks between aaa and bbb; structural match should
        consume all of them."""
        path = os.path.join(tmp_dir, "span_check.py")
        with open(path, "w") as f:
            # aaa, 3 blanks, bbb, ccc, ddd — old_str has aaa, 1 blank, bbb, ccc
            f.write("aaa\n\n\nbbb\nccc\nddd\n")
        result = et.edit_file(path, {
            "old_str": "aaa\n\nbbb\nccc",
            "new_str": "XXX",
        })
        assert "Edited" in result
        with open(path) as f:
            content = f.read()
        assert "XXX" in content
        assert "ddd" in content
        assert "aaa" not in content
        assert "bbb" not in content
        assert "ccc" not in content

