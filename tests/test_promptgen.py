import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import promptgen as pg


def plain(value):
    value = re.sub(r"\x1b\[[0-9;]*m", "", value)
    return value.replace("\x01", "").replace("\x02", "")


class ConfigTests(unittest.TestCase):
    def test_invalid_fields_and_types(self):
        for segment in [
            {"width": True}, {"space_before": False}, {"hide_empty": "false"},
            {"prefix": 1}, {"fg": "256,0,0"}, {"effects": ["unknown"]},
            {"content": {"text": "a", "eval": "b"}}, {"widht": 5},
            {"content": {"eval": {"fish": "x"}}}, {"content": {"eval": {"bash": None}}},
            {"content": "a\nb"}, {"newline": True, "content": "lost"},
            {"content": {"eval": "x"}, "width": 2},
            {"fg": "0,0,0 - 1,1,1"}, {"prefix": "long", "width": 1},
        ]:
            with self.subTest(segment=segment), self.assertRaises(pg.ConfigError):
                pg.normalize_items([segment])

    def test_width_and_gradient(self):
        seg = pg.parse_segment({"content": "가나다", "width": 5,
                                "bg": "0,0,0 - 200,100,0"}, 0)
        self.assertEqual(pg.normalize_static_text(seg), "가나 ")
        self.assertEqual(plain(pg.static_body(seg, "pwsh")), "가나 ")
        self.assertIn("48;2;200;100;0", pg.static_body(seg, "pwsh"))

    def test_shell_selection(self):
        items = pg.normalize_items([{"content": {"eval": {"bash": "x"}}}])
        for shell in ("tcsh", "zsh", "pwsh"):
            with self.subTest(shell=shell), self.assertRaises(pg.ConfigError):
                pg.compile_prompt(items, shell, "config", "output")
        items = pg.normalize_items([{"content": {"eval": {"default": "x"}}}])
        for shell in pg.SHELLS:
            self.assertIn("Generated", pg.compile_prompt(items, shell, "config", "output"))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="promptgen test ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "prompt.json"

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = pg.main([str(arg) for arg in args])
        return status, out.getvalue(), err.getvalue()

    def test_init_and_generate_all(self):
        self.assertEqual(self.call("--init", self.config)[0], 0)
        before = self.config.read_bytes()
        self.assertEqual(self.call("--init", self.config)[0], 2)
        self.assertEqual(self.config.read_bytes(), before)
        output = self.root / "generated"
        self.assertEqual(self.call(self.config, "--shell", "all", "--check", "--output-dir", output)[0], 0)
        self.assertFalse(output.exists())
        self.assertEqual(self.call(self.config, "--shell", "all", "--output-dir", output)[0], 0)
        self.assertEqual({p.suffix for p in output.iterdir()}, {".bash", ".tcsh", ".zsh", ".ps1"})
        for file in output.iterdir():
            self.assertNotIn(b"\r\n", file.read_bytes())

    def test_check_requires_selected_shell(self):
        self.config.write_text(json.dumps({"segments": [{"content": {"eval": {"bash": "x"}}}]}))
        self.assertEqual(self.call(self.config, "--shell", "zsh", "--check")[0], 2)
        output = self.root / "out"
        self.assertEqual(self.call(self.config, "--shell", "all", "--output-dir", output)[0], 2)
        self.assertFalse(output.exists())

    def test_output_cannot_replace_config(self):
        self.call("--init", self.config)
        before = self.config.read_bytes()
        self.assertEqual(self.call(self.config, "--shell", "bash", "-o", self.config)[0], 2)
        self.assertEqual(self.config.read_bytes(), before)

    def test_invalid_json_and_version(self):
        for data in ["{", '{"version": true, "segments": []}', '{"segment": []}']:
            self.config.write_text(data)
            self.assertEqual(self.call(self.config, "--shell", "bash", "--check")[0], 2)


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="promptgen shell ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def shell(self, name):
        if os.name == "nt" and name != "pwsh":
            self.skipTest("Run POSIX shells under Linux/WSL")
        exe = shutil.which(name)
        if not exe:
            self.skipTest(f"{name} is not installed")
        return exe

    def run_prompt(self, shell, segments, setup="", before="", after=""):
        exe = self.shell(shell)
        target = self.root / ("prompt." + pg.EXTENSIONS[shell])
        pg.atomic_write(target, pg.compile_prompt(pg.normalize_items(segments), shell, "test", str(target)))
        if shell == "pwsh":
            script = setup + f"\n. {pg.quote_pwsh(str(target))}\n" + before + "\n[Console]::Write((prompt))\n" + after
            argv = [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-File"]
        elif shell == "tcsh":
            script = setup + f"\nset promptgen_source = {pg.quote_tcsh(str(target))}\nsource \"$promptgen_source\"\n" + before
            script += '\nprintf "%s" $prompt:q\n' + after
            argv = [exe, "-f"]
        else:
            script = setup + f"\nsource {pg.quote_bash(str(target))}\n" + before
            script += ('\nprintf "%s" "${PS1@P}"\n' if shell == "bash" else '\nprint -Pn -- "$PROMPT"\n') + after
            argv = [exe, "--noprofile", "--norc"] if shell == "bash" else [exe, "-f"]
        script_path = self.root / ("run." + pg.EXTENSIONS[shell])
        script_path.write_text(script, encoding="utf-8", newline="\n")
        result = subprocess.run([*argv, str(script_path)], capture_output=True, text=True,
                                encoding="utf-8", timeout=20, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        out = result.stdout
        if shell == "tcsh":
            # Noninteractive tcsh exposes the raw prompt including width markers.
            out = out.replace("%{", "").replace("%}", "").replace("%b", "")
        return plain(out)

    def basic(self, shell):
        segments = [
            {"content": "LEFT", "prefix": "[", "suffix": ">", "bg": "10,20,30"},
            {"content": {"eval": {"default": ""}}, "hide_empty": True, "prefix": "HIDDEN"},
            {"content": {"eval": {"default": "RIGHT"}}, "prefix": "DROP", "suffix": "]",
             "connect": "previous", "bg": "30,40,50"},
            "newline", {"content": "가나", "width": 5, "fg": "0,0,0 - 200,100,50"},
        ]
        self.assertEqual(self.run_prompt(shell, segments), "[LEFT>RIGHT]\n가나 ")

    def test_bash_render(self):
        self.basic("bash")

    def test_zsh_render(self):
        self.basic("zsh")

    def test_tcsh_render(self):
        self.basic("tcsh")

    def test_pwsh_render(self):
        self.basic("pwsh")

    def literal(self, shell):
        text = "'quotes' \"double\" $USER $(printf BAD) `printf BAD` \\u %n ! end"
        out = self.run_prompt(shell, [{"prefix": "$", "content": text, "suffix": "!"}])
        if shell == "tcsh":
            out = out.replace("%%", "%").replace(r"\!", "!")
        self.assertEqual(out, "$" + text + "!")

    def test_bash_literals(self):
        self.literal("bash")

    def test_zsh_literals(self):
        self.literal("zsh")

    def test_tcsh_literals(self):
        self.literal("tcsh")

    def test_pwsh_literals(self):
        self.literal("pwsh")

    def test_bash_native_and_status_hooks(self):
        segments = [{"content": {"eval": "${PROMPTGEN_STATUS}:\\\\u"}}]
        out = self.run_prompt("bash", segments, setup="PROMPT_COMMAND='seen=$?'",
                              before="source ./prompt.bash\n(exit 7)\neval \"$PROMPT_COMMAND\"",
                              after='printf "|%s|%s" "$seen" "$PROMPT_COMMAND"')
        self.assertRegex(out, r"^7:[^|]+\|7\|__promptgen_update;seen=\$\?$")

    def test_bash_array_hooks(self):
        out = self.run_prompt("bash", [{"content": "ok"}],
                              setup="PROMPT_COMMAND=('seen=$?' 'other=kept')",
                              before='source ./prompt.bash\n(exit 9)\nfor hook in "${PROMPT_COMMAND[@]}"; do eval "$hook"; done',
                              after='printf "|%s|%s|%s" "$seen" "$other" "${#PROMPT_COMMAND[@]}"')
        self.assertEqual(out, "ok|9|kept|3")

    def test_zsh_hooks_and_native(self):
        out = self.run_prompt("zsh", [{"content": {"eval": "%n"}}],
                              setup="old_hook() { :; }; precmd_functions=(old_hook)",
                              before="source ./prompt.zsh",
                              after='print -rn -- "|${(j:,:)precmd_functions}"')
        self.assertRegex(out, r"^[^|]+\|__promptgen_update,old_hook$")

    def test_eval_results_not_executed_twice(self):
        self.shell("bash")
        for shell in ("bash", "zsh"):
            if os.name == "nt" or not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                value = "$(printf '$(touch should-not-exist)')"
                self.assertEqual(self.run_prompt(shell, [{"content": {"eval": value}}]), "$(touch should-not-exist)")
                self.assertFalse((self.root / "should-not-exist").exists())

    def test_pwsh_status_and_exitcode(self):
        out = self.run_prompt("pwsh", [{"content": {"eval": "$PROMPTGEN_STATUS/$PROMPTGEN_LASTEXITCODE"}}],
                              before="$global:LASTEXITCODE = 42\nWrite-Error test -ErrorAction SilentlyContinue",
                              after='[Console]::Write("|$global:LASTEXITCODE")')
        self.assertEqual(out, "1/42|42")

    def test_tcsh_status_and_repeat_source(self):
        out = self.run_prompt("tcsh", [{"content": {"eval": "${promptgen_status}"}}],
                              before='source "$promptgen_source"\n(exit 7)\nprecmd\n')
        self.assertEqual(out, "7")

    def test_command_substitutions(self):
        available = [s for s in pg.SHELLS if shutil.which(s) and (os.name != "nt" or s == "pwsh")]
        if not available:
            self.skipTest("No supported shells installed")
        for shell in available:
            expr = {"bash": "$(printf VALUE)", "zsh": "$(printf VALUE)",
                    "tcsh": "`printf VALUE`", "pwsh": "$('VALUE')"}[shell]
            with self.subTest(shell=shell):
                self.assertEqual(self.run_prompt(shell, [{"content": {"eval": expr}}]), "VALUE")

    @unittest.skipIf(os.name == "nt", "PTY test requires Linux/macOS")
    def test_tcsh_interactive_literal_and_newline(self):
        import pty
        import select
        import termios
        import time

        exe = self.shell("tcsh")
        target = self.root / "interactive.tcsh"
        segments = [{"content": "LITERAL %n ! $USER"}, "newline", {"content": "READY> "}]
        pg.atomic_write(target, pg.compile_prompt(pg.normalize_items(segments), "tcsh", "test", str(target)))
        master, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        proc = subprocess.Popen([exe, "-f", "-i"], stdin=slave, stdout=slave, stderr=slave,
                                env={**os.environ, "TERM": "dumb"}, cwd=self.root)
        os.close(slave)
        data = b""
        try:
            command = f'set promptgen_source = {pg.quote_tcsh(str(target))}; source "$promptgen_source"\n'
            os.write(master, command.encode())
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    data += os.read(master, 65536)
                    if b"READY> " in data:
                        break
            self.assertIn("LITERAL %n ! $USER\nREADY> ", plain(data.decode(errors="replace")).replace("\r", ""))
        finally:
            proc.kill()
            proc.wait(timeout=5)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
