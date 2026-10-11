#!/usr/bin/env python3
"""Compile YAML/JSON prompts to Bash, tcsh, Zsh and PowerShell.

Static colors, widths and gradients are computed at build time.
Only generated shell code and configured commands run at prompt time.
See README.md for the schema and shell installation instructions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None

try:
    import regex as regex_mod  # type: ignore
except Exception:  # pragma: no cover
    regex_mod = None

try:
    from wcwidth import wcswidth  # type: ignore
except Exception:  # pragma: no cover
    wcswidth = None


class ConfigError(ValueError):
    pass


SHELLS = ("bash", "tcsh", "zsh", "pwsh")
EXTENSIONS = {"bash": "bash", "tcsh": "tcsh", "zsh": "zsh", "pwsh": "ps1"}


def check_keys(raw: dict, allowed: set[str], where: str) -> None:
    unknown = raw.keys() - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown fields: {', '.join(sorted(map(str, unknown)))}")


def literal_string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where}: must be a string")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigError(f"{where}: control characters are not allowed; use a newline item")
    return value


@dataclass(frozen=True)
class RGB:
    r: int
    g: int
    b: int

    def semi(self) -> str:
        return f"{self.r};{self.g};{self.b}"


@dataclass(frozen=True)
class ColorSpec:
    start: RGB
    end: RGB

    @property
    def is_gradient(self) -> bool:
        return self.start != self.end

    def at(self, index: int, total: int) -> RGB:
        if total <= 1:
            return self.start
        t = index / (total - 1)
        return RGB(
            round(self.start.r + (self.end.r - self.start.r) * t),
            round(self.start.g + (self.end.g - self.start.g) * t),
            round(self.start.b + (self.end.b - self.start.b) * t),
        )


@dataclass
class Segment:
    index: int
    prefix: str
    suffix: str
    content_kind: str  # text | eval
    content: Any
    space_before: int
    space_after: int
    fg: Optional[ColorSpec]
    bg: Optional[ColorSpec]
    effects: tuple[str, ...]
    width: Optional[int]
    hide_empty: bool
    connect_previous: bool

    def eval_for(self, shell: str) -> str:
        if self.content_kind != "eval":
            raise ConfigError("eval_for called for non-eval segment")
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, dict):
            value = self.content.get(shell)
            if value is None:
                value = self.content.get("default")
            if value is None:
                raise ConfigError(
                    f"segment {self.index}: content.eval has no value for {shell!r} and no default"
                )
            if not isinstance(value, str):
                raise ConfigError(f"segment {self.index}: content.eval.{shell} must be a string")
            return value
        raise ConfigError(f"segment {self.index}: invalid eval value")


EFFECT_CODES = {
    "bold": 1,
    "dim": 2,
    "italic": 3,
    "underline": 4,
    "blink": 5,
    "reverse": 7,
    "hidden": 8,
    "strike": 9,
}

RGB_RE = re.compile(r"^\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*$")


def parse_rgb(value: str, where: str) -> RGB:
    m = RGB_RE.match(value)
    if not m:
        raise ConfigError(f"{where}: expected r,g,b, got {value!r}")
    vals = tuple(int(x) for x in m.groups())
    if any(v < 0 or v > 255 for v in vals):
        raise ConfigError(f"{where}: RGB values must be 0..255, got {value!r}")
    return RGB(*vals)


def parse_color(value: Any, where: str) -> Optional[ColorSpec]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{where}: color must be a string like '1,2,3' or '1,2,3 - 4,5,6'")
    parts = [p.strip() for p in value.split("-")]
    if len(parts) == 1:
        rgb = parse_rgb(parts[0], where)
        return ColorSpec(rgb, rgb)
    if len(parts) == 2:
        return ColorSpec(parse_rgb(parts[0], where), parse_rgb(parts[1], where))
    raise ConfigError(f"{where}: invalid gradient syntax {value!r}")


def graphemes(text: str) -> list[str]:
    if regex_mod is not None:
        return regex_mod.findall(r"\X", text)
    return list(text)


def cell_width(text: str) -> int:
    if wcswidth is not None:
        n = wcswidth(text)
        if n >= 0:
            return n
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
    return width


def truncate_to_cells(text: str, cells: int) -> str:
    if cells <= 0:
        return ""
    out: list[str] = []
    used = 0
    for g in graphemes(text):
        w = max(0, cell_width(g))
        if used + w > cells:
            break
        out.append(g)
        used += w
    return "".join(out)


def normalize_static_text(seg: Segment) -> str:
    content = str(seg.content)
    prefix_w = cell_width(seg.prefix)
    suffix_w = cell_width(seg.suffix)
    fixed = seg.space_before + seg.space_after + prefix_w + suffix_w
    if seg.width is None:
        return " " * seg.space_before + seg.prefix + content + seg.suffix + " " * seg.space_after
    if seg.width < fixed:
        raise ConfigError(
            f"segment {seg.index}: width={seg.width} is smaller than prefix/suffix/padding width {fixed}"
        )
    body_cells = seg.width - fixed
    content = truncate_to_cells(content, body_cells)
    pad = body_cells - cell_width(content)
    return " " * seg.space_before + seg.prefix + content + (" " * pad) + seg.suffix + " " * seg.space_after


def parse_content(raw: Any, index: int) -> tuple[str, Any]:
    if isinstance(raw, str):
        return "text", literal_string(raw, f"segment {index}.content")
    if not isinstance(raw, dict):
        raise ConfigError(f"segment {index}: content must be a string or mapping")
    check_keys(raw, {"text", "eval"}, f"segment {index}.content")
    has_text = "text" in raw
    has_eval = "eval" in raw
    if has_text == has_eval:
        raise ConfigError(f"segment {index}: content must contain exactly one of text/eval")
    if has_text:
        if not isinstance(raw["text"], str):
            raise ConfigError(f"segment {index}: content.text must be a string")
        return "text", literal_string(raw["text"], f"segment {index}.content.text")
    val = raw["eval"]
    if not isinstance(val, (str, dict)):
        raise ConfigError(f"segment {index}: content.eval must be a string or shell mapping")
    if isinstance(val, dict):
        check_keys(val, set(SHELLS) | {"default"}, f"segment {index}.content.eval")
        if not val:
            raise ConfigError(f"segment {index}: content.eval cannot be empty")
        for shell, expr in val.items():
            literal_string(expr, f"segment {index}.content.eval.{shell}")
    else:
        literal_string(val, f"segment {index}.content.eval")
    return "eval", val


def parse_segment(raw: dict[str, Any], index: int) -> Segment:
    check_keys(raw, {"prefix", "suffix", "content", "space_before", "space_after",
                     "fg", "bg", "effects", "width", "hide_empty", "connect"},
               f"segment {index}")
    content_kind, content = parse_content(raw.get("content", ""), index)
    effects_raw = raw.get("effects", [])
    if isinstance(effects_raw, str):
        effects_raw = [effects_raw]
    if not isinstance(effects_raw, list) or not all(isinstance(x, str) for x in effects_raw):
        raise ConfigError(f"segment {index}: effects must be a string or list of strings")
    unknown = [x for x in effects_raw if x not in EFFECT_CODES]
    if unknown:
        raise ConfigError(f"segment {index}: unknown ANSI effects: {', '.join(unknown)}")

    width = raw.get("width")
    if width is not None:
        if type(width) is not int or width <= 0:
            raise ConfigError(f"segment {index}: width must be a positive integer or null")

    def nonneg_int(name: str, default: int = 0) -> int:
        v = raw.get(name, default)
        if type(v) is not int or v < 0:
            raise ConfigError(f"segment {index}: {name} must be a non-negative integer")
        return v

    connect = raw.get("connect")
    if connect not in (None, "previous"):
        raise ConfigError(f"segment {index}: connect must be 'previous' or omitted")
    if type(raw.get("hide_empty", False)) is not bool:
        raise ConfigError(f"segment {index}: hide_empty must be a boolean")

    seg = Segment(
        index=index,
        prefix=literal_string(raw.get("prefix", ""), f"segment {index}.prefix"),
        suffix=literal_string(raw.get("suffix", ""), f"segment {index}.suffix"),
        content_kind=content_kind,
        content=content,
        space_before=nonneg_int("space_before"),
        space_after=nonneg_int("space_after"),
        fg=parse_color(raw.get("fg"), f"segment {index}.fg"),
        bg=parse_color(raw.get("bg"), f"segment {index}.bg"),
        effects=tuple(effects_raw),
        width=width,
        hide_empty=bool(raw.get("hide_empty", False)),
        connect_previous=(connect == "previous"),
    )

    if ((seg.fg and seg.fg.is_gradient) or (seg.bg and seg.bg.is_gradient)) and seg.width is None:
        raise ConfigError(
            f"segment {index}: gradients require width so color steps are deterministic"
        )

    if seg.content_kind == "eval":
        if (seg.fg and seg.fg.is_gradient) or (seg.bg and seg.bg.is_gradient):
            raise ConfigError(
                f"segment {index}: dynamic eval content cannot use per-cell gradients in this MVP; "
                "use fixed fg/bg or static text"
            )
        if seg.width is not None:
            raise ConfigError(
                f"segment {index}: dynamic eval content cannot use fixed width in this MVP; "
                "runtime width handling would add per-prompt overhead"
            )
    else:
        normalize_static_text(seg)  # validates width early
    return seg


def load_config(path: Path) -> tuple[dict[str, Any], list[Any]]:
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(text)
    elif suffix in {".yaml", ".yml"}:
        if yaml is None:
            raise ConfigError("PyYAML is required for YAML configs: pip install pyyaml")
        data = yaml.safe_load(text)
    else:
        # Convenient for ~/.prompt with no extension: try YAML first, then JSON.
        if yaml is not None:
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
    if not isinstance(data, dict):
        raise ConfigError("top-level config must be a mapping/object")
    check_keys(data, {"version", "segments", "items"}, "config")
    if "segments" in data and "items" in data:
        raise ConfigError("use segments or items, not both")
    version = data.get("version", 1)
    if type(version) is not int or version != 1:
        raise ConfigError(f"unsupported config version: {version!r}")
    items = data.get("segments", data.get("items"))
    if not isinstance(items, list):
        raise ConfigError("config must contain a 'segments' list")
    return data, items


def normalize_items(items: list[Any]) -> list[Any]:
    out: list[Any] = []
    seg_index = 0
    for item in items:
        if item == "newline" or (isinstance(item, dict) and item.get("newline") is True):
            if isinstance(item, dict):
                check_keys(item, {"newline"}, "newline item")
            out.append("newline")
            continue
        if not isinstance(item, dict):
            raise ConfigError(f"segments[{len(out)}]: expected mapping or 'newline'")
        out.append(parse_segment(item, seg_index))
        seg_index += 1
    return out


def effect_codes(effects: Iterable[str]) -> list[int]:
    return [EFFECT_CODES[x] for x in effects]


def rgb_fg(rgb: Optional[RGB]) -> Optional[str]:
    return None if rgb is None else f"38;2;{rgb.semi()}"


def rgb_bg(rgb: Optional[RGB]) -> Optional[str]:
    return None if rgb is None else f"48;2;{rgb.semi()}"


def sgr_params(effects: Iterable[str], fg: Optional[RGB], bg: Optional[RGB]) -> str:
    parts = [str(x) for x in effect_codes(effects)]
    f = rgb_fg(fg)
    b = rgb_bg(bg)
    if f:
        parts.append(f)
    if b:
        parts.append(b)
    return ";".join(parts) if parts else "0"


def bash_np(params: str) -> str:
    # Readline markers survive the single indirect expansion of PS1.
    return f"\x01\x1b[{params}m\x02"


def tcsh_np(params: str) -> str:
    # tcsh %{...%} emits its contents literally. Embed a real ESC byte here so
    # ANSI survives intermediate variable concatenation instead of appearing as "\\033".
    return f"%{{\x1b[{params}m%}}"


def reset(shell: str) -> str:
    # tcsh documents that %{...%} should not be the final prompt sequence; %b is
    # a harmless native prompt escape after the ANSI reset and also ends bold.
    return shell_np(shell, "0") + ("%b" if shell == "tcsh" else "")


def shell_np(shell: str, params: str) -> str:
    if shell == "bash":
        return bash_np(params)
    if shell == "pwsh":
        return f"\x1b[{params}m"
    return tcsh_np(params)


def quote_bash(s: str) -> str:
    # single-quoted shell literal
    return "'" + s.replace("'", "'\"'\"'") + "'"


def quote_tcsh(s: str) -> str:
    # C-style literals (tcsh >= 6.24) avoid interpolation and source newlines.
    s = s.replace("\\", "\\\\").replace("'", "\\'").replace("!", r"\041")
    return "$'" + s.replace("\n", "\\n").replace("\r", "\\r") + "'"


def quote_pwsh(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def prompt_literal(shell: str, s: str) -> str:
    if shell in {"tcsh", "zsh"}:
        s = s.replace("%", "%%")
    if shell == "tcsh":
        s = s.replace("!", r"\!")
    return s


def shell_quote(shell: str, s: str) -> str:
    return quote_bash(s) if shell == "bash" else quote_tcsh(s)


def ansi_piece(shell: str, text: str, effects: Iterable[str], fg: Optional[RGB], bg: Optional[RGB]) -> str:
    if not text:
        return ""
    return shell_np(shell, sgr_params(effects, fg, bg)) + prompt_literal(shell, text) + reset(shell)


def bg_start(seg: Segment) -> Optional[RGB]:
    return seg.bg.start if seg.bg else None


def bg_end(seg: Segment) -> Optional[RGB]:
    return seg.bg.end if seg.bg else None


def _gradient_total(seg: Segment) -> int:
    if seg.width is not None:
        return seg.width
    return max(
        1,
        seg.space_before
        + cell_width(seg.prefix)
        + cell_width(str(seg.content) if seg.content_kind == "text" else "")
        + cell_width(seg.suffix)
        + seg.space_after,
    )


def _bg_at(seg: Segment, pos: int) -> Optional[RGB]:
    return seg.bg.at(max(0, min(pos, _gradient_total(seg) - 1)), _gradient_total(seg)) if seg.bg else None


def _fg_at(seg: Segment, pos: int) -> Optional[RGB]:
    return seg.fg.at(max(0, min(pos, _gradient_total(seg) - 1)), _gradient_total(seg)) if seg.fg else None


def plain_prefix(seg: Segment, shell: str) -> str:
    if not seg.prefix:
        return ""
    pos = seg.space_before
    # Powerline-style edge: prefix foreground follows segment background.
    fg = _bg_at(seg, pos) or _fg_at(seg, pos)
    return ansi_piece(shell, seg.prefix, seg.effects, fg, None)


def normal_suffix(seg: Segment, shell: str) -> str:
    if not seg.suffix:
        return ""
    total = _gradient_total(seg)
    pos = max(0, total - seg.space_after - cell_width(seg.suffix))
    fg = _bg_at(seg, pos) or _fg_at(seg, pos)
    return ansi_piece(shell, seg.suffix, seg.effects, fg, None)


def bridge_suffix(prev: Segment, current: Segment, shell: str) -> str:
    if not prev.suffix:
        return ""
    prev_total = _gradient_total(prev)
    pos = max(0, prev_total - prev.space_after - cell_width(prev.suffix))
    fg = _bg_at(prev, pos) or _fg_at(prev, pos)
    bg = bg_start(current)
    return ansi_piece(shell, prev.suffix, prev.effects, fg, bg)


def static_body(seg: Segment, shell: str) -> str:
    content = str(seg.content)
    prefix_w = cell_width(seg.prefix)
    suffix_w = cell_width(seg.suffix)
    if seg.width is None:
        body = content
        body_cells = cell_width(body)
        total_cells = max(1, seg.space_before + prefix_w + body_cells + suffix_w + seg.space_after)
    else:
        fixed = seg.space_before + seg.space_after + prefix_w + suffix_w
        body_cells = seg.width - fixed
        body = truncate_to_cells(content, body_cells)
        body += " " * (body_cells - cell_width(body))
        total_cells = seg.width

    if not body:
        return ""

    body_offset = seg.space_before + prefix_w

    # Fast path: no gradient => one ANSI run.
    if not ((seg.fg and seg.fg.is_gradient) or (seg.bg and seg.bg.is_gradient)):
        fg = seg.fg.start if seg.fg else None
        bg = seg.bg.start if seg.bg else None
        return ansi_piece(shell, body, seg.effects, fg, bg)

    out: list[str] = []
    cell_pos = 0
    for g in graphemes(body):
        w = max(1, cell_width(g))
        absolute = min(body_offset + cell_pos, total_cells - 1)
        fg = seg.fg.at(absolute, total_cells) if seg.fg else None
        bg = seg.bg.at(absolute, total_cells) if seg.bg else None
        out.append(ansi_piece(shell, g, seg.effects, fg, bg))
        cell_pos += w
    return "".join(out)


def dynamic_body_wrappers(seg: Segment, shell: str) -> tuple[str, str]:
    fg = seg.fg.start if seg.fg else None
    bg = seg.bg.start if seg.bg else None
    pre = shell_np(shell, sgr_params(seg.effects, fg, bg))
    post = reset(shell)
    return pre, post


def render_static_full(seg: Segment, shell: str) -> tuple[str, str, str]:
    return plain_prefix(seg, shell), static_body(seg, shell), normal_suffix(seg, shell)


def bash_eval_assignment(name: str, expr: str) -> str:
    # expr is intentionally raw shell syntax controlled by the user.
    return f'{name}="{expr}"'


def tcsh_eval_assignment(name: str, expr: str) -> str:
    return f'set {name} = "{expr}"'


def generate_posix(items: list[Any], source_name: str, shell: str) -> str:
    """Bash and Zsh share assembly logic, with separate prompt expansion/hooks."""
    q = quote_bash
    lines = [
        "# Generated by promptgen.py. Do not edit by hand.",
        f"# Source: {source_name!r}",
        "__promptgen_update() {",
        "  local __pg_status=$?",
    ]
    if shell == "zsh":
        lines += ["  emulate -L zsh"]
    lines += [
        "  PROMPTGEN_STATUS=$__pg_status",
        "  local __pg_out='' __pg_value=''",
        "  local __pg_prev_visible=0",
        "  local __pg_prev_suffix='' __pg_prev_suffix_normal='' __pg_prev_bg='' __pg_prev_space_after=''",
    ]
    for seg in items:
        if seg == "newline":
            lines += [
                "  if (( __pg_prev_visible )); then __pg_out+=$__pg_prev_suffix_normal$__pg_prev_space_after; fi",
                "  __pg_out+=$'\\n'",
                "  __pg_prev_visible=0",
            ]
            continue
        lead = " " * seg.space_before
        prefix = lead + plain_prefix(seg, shell)
        normal_suf = normal_suffix(seg, shell)
        if seg.content_kind == "text":
            visible = not (seg.hide_empty and seg.content == "")
            lines.append(f"  if {'true' if visible else 'false'}; then")
        else:
            lines.append(f"  {bash_eval_assignment('__pg_value', seg.eval_for(shell))}")
            if shell == "bash":
                # Decode native \u/\w/... without executing command output again.
                lines += [
                    "  shopt -u promptvars",
                    '  __pg_value=${__pg_value@P}',
                    "  shopt -s promptvars",
                ]
            lines.append("  if [[ -n $__pg_value ]]; then" if seg.hide_empty else "  if true; then")
        if seg.connect_previous:
            lines.append("    if (( __pg_prev_visible )); then")
            if bg_start(seg):
                bridge = shell_np(shell, f"38;2;${{__pg_prev_bg}};48;2;{bg_start(seg).semi()}")
                bridge += "${__pg_prev_suffix}" + reset(shell)
                lines.append(f'      if [[ -n $__pg_prev_suffix ]]; then __pg_out+="{bridge}"; fi')
            else:
                lines.append("      __pg_out+=$__pg_prev_suffix_normal")
            lines += [
                "      __pg_out+=$__pg_prev_space_after",
                f"      __pg_out+={q(lead)}",
                "    else",
                f"      __pg_out+={q(prefix)}",
                "    fi",
            ]
        else:
            lines += [
                "    if (( __pg_prev_visible )); then __pg_out+=$__pg_prev_suffix_normal$__pg_prev_space_after; fi",
                f"    __pg_out+={q(prefix)}",
            ]
        if seg.content_kind == "text":
            lines.append(f"    __pg_out+={q(static_body(seg, shell))}")
        else:
            pre, post = dynamic_body_wrappers(seg, shell)
            lines.append(f'    __pg_out+={q(pre)}"$__pg_value"{q(post)}')
        color = bg_end(seg) or (seg.fg.end if seg.fg else RGB(255, 255, 255))
        lines += [
            f"    __pg_prev_suffix={q(prompt_literal(shell, seg.suffix))}",
            f"    __pg_prev_suffix_normal={q(normal_suf)}",
            f"    __pg_prev_bg={q(color.semi())}",
            f"    __pg_prev_space_after={q(' ' * seg.space_after)}",
            "    __pg_prev_visible=1",
            "  fi",
        ]
    lines += [
        "  if (( __pg_prev_visible )); then __pg_out+=$__pg_prev_suffix_normal$__pg_prev_space_after; fi",
        "  typeset -g __promptgen_prompt=$__pg_out",
    ]
    if shell == "bash":
        lines += [
            "  PS1='${__promptgen_prompt}'",
            '  return "$__pg_status"',
            "}",
            "shopt -s promptvars",
            "# Keep existing hooks and their incoming exit status; install once.",
            'if [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == "declare -a "* ]]; then',
            '  __pg_hooks=("${PROMPT_COMMAND[@]}")',
            "  PROMPT_COMMAND=(__promptgen_update)",
            '  for __pg_hook in "${__pg_hooks[@]}"; do',
            '    [[ $__pg_hook == __promptgen_update ]] || PROMPT_COMMAND+=("$__pg_hook")',
            "  done",
            "  unset __pg_hooks __pg_hook",
            "else",
            '  case ${PROMPT_COMMAND:-} in',
            r"    __promptgen_update|__promptgen_update\;*) ;;",
            '    *) PROMPT_COMMAND="__promptgen_update${PROMPT_COMMAND:+;${PROMPT_COMMAND}}" ;;',
            "  esac",
            "fi",
            "__promptgen_update",
            ":",
        ]
    else:
        lines += [
            "  PROMPT='${__promptgen_prompt}'",
            "  return 0",
            "}",
            "setopt promptsubst promptpercent",
            "unsetopt promptbang",
            "# Preserve both precmd and any existing array hooks; avoid duplicates.",
            "typeset -ga precmd_functions",
            'precmd_functions=(__promptgen_update "${(@)precmd_functions:#__promptgen_update}")',
            "__promptgen_update",
        ]
    return "\n".join(lines) + "\n"


def generate_bash(items: list[Any], source_name: str) -> str:
    return generate_posix(items, source_name, "bash")


def generate_zsh(items: list[Any], source_name: str) -> str:
    return generate_posix(items, source_name, "zsh")


def generate_pwsh(items: list[Any], source_name: str) -> str:
    q = quote_pwsh
    lines = [
        "# Generated by promptgen.py. Do not edit by hand.",
        f"# Source: {source_name!r}",
        "#requires -Version 7.0",
        "function global:prompt {",
        "  $__pg_ok = $?",
        "  $__pg_exit = $global:LASTEXITCODE",
        "  $global:PROMPTGEN_SUCCESS = $__pg_ok",
        "  $global:PROMPTGEN_LASTEXITCODE = $__pg_exit",
        "  $global:PROMPTGEN_STATUS = if ($__pg_ok) { 0 } else { 1 }",
        "  $__pg_out = ''",
        "  $__pg_prev_visible = $false",
        "  $__pg_prev_suffix = ''; $__pg_prev_suffix_normal = ''; $__pg_prev_bg = ''; $__pg_prev_space_after = ''",
        "  try {",
    ]
    for seg in items:
        if seg == "newline":
            lines += [
                "    if ($__pg_prev_visible) { $__pg_out += $__pg_prev_suffix_normal + $__pg_prev_space_after }",
                '    $__pg_out += "`n"',
                "    $__pg_prev_visible = $false",
            ]
            continue
        prefix = " " * seg.space_before + plain_prefix(seg, "pwsh")
        if seg.content_kind == "text":
            visible = not (seg.hide_empty and seg.content == "")
            lines.append(f"    if ({'$true' if visible else '$false'}) {{")
        else:
            lines += [f'    $__pg_value = "{seg.eval_for("pwsh")}"',
                      "    if ($__pg_value.Length -gt 0) {" if seg.hide_empty else "    if ($true) {"]
        if seg.connect_previous:
            lines.append("      if ($__pg_prev_visible) {")
            if bg_start(seg):
                lines += [
                    "        if ($__pg_prev_suffix.Length -gt 0) {",
                    f"          $__pg_out += {q(chr(27) + '[38;2;')} + $__pg_prev_bg + {q(';48;2;' + bg_start(seg).semi() + 'm')} + $__pg_prev_suffix + {q(reset('pwsh'))}",
                    "        }",
                ]
            else:
                lines.append("        $__pg_out += $__pg_prev_suffix_normal")
            lines += [
                f"        $__pg_out += $__pg_prev_space_after + {q(' ' * seg.space_before)}",
                "      } else {",
                f"        $__pg_out += {q(prefix)}",
                "      }",
            ]
        else:
            lines += [
                "      if ($__pg_prev_visible) { $__pg_out += $__pg_prev_suffix_normal + $__pg_prev_space_after }",
                f"      $__pg_out += {q(prefix)}",
            ]
        if seg.content_kind == "text":
            lines.append(f"      $__pg_out += {q(static_body(seg, 'pwsh'))}")
        else:
            pre, post = dynamic_body_wrappers(seg, "pwsh")
            lines.append(f"      $__pg_out += {q(pre)} + $__pg_value + {q(post)}")
        color = bg_end(seg) or (seg.fg.end if seg.fg else RGB(255, 255, 255))
        lines += [
            f"      $__pg_prev_suffix = {q(seg.suffix)}",
            f"      $__pg_prev_suffix_normal = {q(normal_suffix(seg, 'pwsh'))}",
            f"      $__pg_prev_bg = {q(color.semi())}",
            f"      $__pg_prev_space_after = {q(' ' * seg.space_after)}",
            "      $__pg_prev_visible = $true",
            "    }",
        ]
    lines += [
        "    if ($__pg_prev_visible) { $__pg_out += $__pg_prev_suffix_normal + $__pg_prev_space_after }",
        "    return $__pg_out",
        "  } finally {",
        "    $global:LASTEXITCODE = $__pg_exit",
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def generate_tcsh(items: list[Any], source_name: str, output_path: str) -> str:
    # tcsh has no shell functions. Generate an update script and install precmd as
    # `source "$promptgen_source" --update`. Do not bake the compiler machine's
    # absolute output path into the file: generated files may be moved/downloaded.
    output_name = Path(output_path).name
    # The top-level sourced file detects --update only when invoked via alias; otherwise it installs the alias.
    lines: list[str] = [
        "# Generated by promptgen.py. Do not edit by hand.",
        f"# Source: {source_name!r}",
        "",
        "set promptgen_status = $status",
        "if ( $#argv > 1 ) then",
        '  if ( "$argv[1]" == "--update" ) set promptgen_status = "$argv[2]"',
        "endif",
        "set __pg_update_mode = 0",
        "if ( $#argv > 0 ) then",
        "  if ( \"$argv[1]\" == \"--update\" ) set __pg_update_mode = 1",
        "endif",
        "if ( $__pg_update_mode ) then",
        "  set __pg_out = \"\"",
        "  set __pg_value = \"\"",
        "  set __pg_prev_visible = 0",
        "  set __pg_prev_suffix = \"\"",
        "  set __pg_prev_suffix_normal = \"\"",
        "  set __pg_prev_bg = \"\"",
        "  set __pg_prev_space_after = \"\"",
        "",
    ]

    seg_no = 0
    # Keep source lines intact; join prompt lines with tcsh C-style literals.
    tcsh_line_vars: list[str] = []
    tcsh_line_no = 0

    for item in items:
        if item == "newline":
            line_var = f"__pg_line_{tcsh_line_no}"
            tcsh_line_vars.append(line_var)
            tcsh_line_no += 1
            lines += [
                "  if ( $__pg_prev_visible ) set __pg_out = \"${__pg_out}${__pg_prev_suffix_normal}${__pg_prev_space_after}\"",
                f'  set {line_var} = "$__pg_out"',
                '  set __pg_out = ""',
                "  set __pg_prev_visible = 0",
                "  set __pg_prev_suffix = \"\"",
                "  set __pg_prev_suffix_normal = \"\"",
                "  set __pg_prev_bg = \"\"",
                "  set __pg_prev_space_after = \"\"",
                "",
            ]
            continue

        seg: Segment = item
        prefix, static_body_rendered, normal_suf = render_static_full(seg, "tcsh") if seg.content_kind == "text" else (plain_prefix(seg, "tcsh"), "", normal_suffix(seg, "tcsh"))
        lead = " " * seg.space_before
        prefix_with_lead = lead + prefix
        this_bg = bg_start(seg)

        lines.append(f"  # segment {seg_no}")
        if seg.content_kind == "text":
            value = str(seg.content)
            lines.append(f"  set __pg_value = {quote_tcsh(value)}")
            visible = not (seg.hide_empty and value == "")
            lines.append(f"  if ( {1 if visible else 0} ) then")
        else:
            expr = seg.eval_for("tcsh")
            lines.append(f"  {tcsh_eval_assignment('__pg_value', expr)}")
            if seg.hide_empty:
                lines.append("  if ( \"$__pg_value\" != \"\" ) then")
            else:
                lines.append("  if ( 1 ) then")

        if seg.connect_previous:
            if this_bg:
                bg_code = this_bg.semi()
                lines += [
                    "    if ( $__pg_prev_visible ) then",
                    "      if ( \"$__pg_prev_suffix\" != \"\" ) then",
                    # This is a double-quoted assignment so variables expand. tcsh prompt's %{ %} hides ANSI width.
                    f"        set __pg_out = \"${{__pg_out}}%{{\x1b[38;2;${{__pg_prev_bg}};48;2;{bg_code}m%}}${{__pg_prev_suffix}}%{{\x1b[0m%}}\"",
                    "      endif",
                    "      set __pg_out = \"${__pg_out}${__pg_prev_space_after}\"",
                    f'      set __pg_out = "${{__pg_out}}"{quote_tcsh(lead)}',
                    "    else",
                    f'      set __pg_out = "${{__pg_out}}"{quote_tcsh(prefix_with_lead)}',
                    "    endif",
                ]
            else:
                lines += [
                    "    if ( $__pg_prev_visible ) then",
                    "      set __pg_out = \"${__pg_out}${__pg_prev_suffix_normal}${__pg_prev_space_after}\"",
                    f'      set __pg_out = "${{__pg_out}}"{quote_tcsh(lead)}',
                    "    else",
                    f'      set __pg_out = "${{__pg_out}}"{quote_tcsh(prefix_with_lead)}',
                    "    endif",
                ]
        else:
            lines += [
                "    if ( $__pg_prev_visible ) set __pg_out = \"${__pg_out}${__pg_prev_suffix_normal}${__pg_prev_space_after}\"",
                f'    set __pg_out = "${{__pg_out}}"{quote_tcsh(prefix_with_lead)}',
            ]

        if seg.content_kind == "text":
            lines.append(f'    set __pg_out = "${{__pg_out}}"{quote_tcsh(static_body_rendered)}')
        else:
            pre, post = dynamic_body_wrappers(seg, "tcsh")
            lines.append(
                f'    set __pg_out = "${{__pg_out}}"{quote_tcsh(pre)}"${{__pg_value}}"{quote_tcsh(post)}'
            )

        prev_rgb = bg_end(seg) or (seg.fg.end if seg.fg else RGB(255, 255, 255))
        lines += [
            f"    set __pg_prev_suffix = {quote_tcsh(prompt_literal('tcsh', seg.suffix))}",
            f"    set __pg_prev_suffix_normal = {_tcsh_sq(normal_suf)}",
            f"    set __pg_prev_bg = {_tcsh_sq(prev_rgb.semi())}",
            f"    set __pg_prev_space_after = {_tcsh_sq(' ' * seg.space_after)}",
            "    set __pg_prev_visible = 1",
            "  endif",
            "",
        ]
        seg_no += 1

    # Save the last logical line. tcsh 6.24 supports C-style $'...'
    # strings, where \n is converted to an actual newline byte. Build the final
    # prompt from adjacent quoted fragments so the newline is data in $prompt,
    # not a prompt escape hidden inside an intermediate variable and not a
    # backslash-newline source continuation.
    final_line_var = f"__pg_line_{tcsh_line_no}"
    tcsh_line_vars.append(final_line_var)
    prompt_expr = "$'\\n'".join(f'"${{{name}}}"' for name in tcsh_line_vars)
    unset_line_vars = " ".join(tcsh_line_vars)

    lines += [
        "  if ( $__pg_prev_visible ) set __pg_out = \"${__pg_out}${__pg_prev_suffix_normal}${__pg_prev_space_after}\"",
        f'  set {final_line_var} = "$__pg_out"',
        f'  set prompt = {prompt_expr}',
        "  unset __pg_out __pg_value __pg_prev_visible __pg_prev_suffix __pg_prev_suffix_normal __pg_prev_bg __pg_prev_space_after",
        f"  unset {unset_line_vars}",
        "else",
        "  # First source: install/update the precmd hook.",
        "  # If promptgen_source was not supplied by .cshrc/.tcshrc, assume this",
        "  # file is being sourced from the current directory. This keeps generated",
        "  # files portable instead of embedding the build machine's absolute path.",
        "  if ( ! $?promptgen_source ) then",
        f'    set promptgen_source = "$cwd/"{quote_tcsh(output_name)}',
        "  endif",
        "  if ( -f \"$promptgen_source\" ) then",
        "    alias precmd 'source \"$promptgen_source\" --update $status'",
        "    source \"$promptgen_source\" --update",
        "  else",
        '    echo "promptgen: cannot find generated file: $promptgen_source" > /dev/stderr',
        '    echo "promptgen: set promptgen_source to the absolute path before sourcing it." > /dev/stderr',
        "  endif",
        "endif",
        "unset __pg_update_mode",
        "",
    ]
    return "\n".join(lines)


def _tcsh_sq(s: str) -> str:
    return quote_tcsh(s)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def example_config() -> dict[str, Any]:
    """A portable starter; prompt-time Python is never required."""
    return {
        "version": 1,
        "segments": [
            {"prefix": "", "suffix": "",
             "content": {"eval": {"bash": r"\\t", "tcsh": "%T", "zsh": "%*",
                                   "pwsh": "$(Get-Date -Format HH:mm:ss)"}},
             "fg": "255,255,255", "bg": "45,90,160", "effects": ["bold"]},
            {"prefix": "", "suffix": "", "connect": "previous",
             "content": {"eval": {"bash": r"\\u@\\h", "tcsh": "%n@%m", "zsh": "%n@%m",
                                   "pwsh": "$([Environment]::UserName)@$([Environment]::MachineName)"}},
             "fg": "255,255,255", "bg": "70,130,190"},
            {"prefix": "", "suffix": "", "connect": "previous",
             "content": {"eval": {"bash": r"\\w", "tcsh": "%~", "zsh": "%~",
                                   "pwsh": "$($executionContext.SessionState.Path.CurrentLocation)"}},
             "fg": "255,255,255", "bg": "40,120,100"},
            {"prefix": "", "suffix": "", "connect": "previous",
             "content": {"text": " PROMPT "}, "width": 14,
             "fg": "255,255,255 - 220,255,240", "bg": "40,120,100 - 80,190,140"},
            "newline",
            {"content": {"text": "❯ "}, "fg": "100,220,150", "effects": ["bold"]},
        ],
    }


def compile_prompt(items: list[Any], shell: str, source_name: str, output_path: str) -> str:
    # Validate selected shell expressions even for --check and hidden segments.
    for item in items:
        if isinstance(item, Segment) and item.content_kind == "eval":
            item.eval_for(shell)
    if shell == "bash":
        return generate_bash(items, source_name)
    if shell == "tcsh":
        return generate_tcsh(items, source_name, output_path)
    if shell == "zsh":
        return generate_zsh(items, source_name)
    if shell == "pwsh":
        return generate_pwsh(items, source_name)
    raise ConfigError(f"unsupported shell: {shell}")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Compile YAML/JSON prompts to Bash, tcsh, Zsh and PowerShell source files")
    p.add_argument("config", type=Path, nargs="?")
    p.add_argument("--init", type=Path, metavar="CONFIG", help="create a starter .json/.yaml file; never overwrite")
    p.add_argument("--shell", choices=[*SHELLS, "all"])
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--output-dir", type=Path, help="write prompt.generated.<extension> files here")
    p.add_argument("--check", action="store_true", help="validate only; do not write output")
    args = p.parse_args(argv)
    if args.init:
        if args.config or args.shell or args.output or args.output_dir or args.check:
            p.error("--init cannot be combined with build options")
    else:
        if args.config is None or args.shell is None:
            p.error("config and --shell are required")
        if args.output and args.output_dir:
            p.error("use --output or --output-dir, not both")
        if args.shell == "all" and args.output:
            p.error("--shell all requires --output-dir instead of --output")
        if not args.check and not (args.output or args.output_dir):
            p.error("--output or --output-dir is required unless --check is used")

    try:
        if args.init:
            path = args.init.expanduser()
            if path.suffix.lower() in {".yaml", ".yml"}:
                if yaml is None:
                    raise ConfigError("PyYAML is required for YAML: pip install pyyaml; or use --init prompt.json")
                text = yaml.safe_dump(example_config(), allow_unicode=True, sort_keys=False)
            elif path.suffix.lower() == ".json":
                text = json.dumps(example_config(), ensure_ascii=False, indent=2) + "\n"
            else:
                raise ConfigError("--init file must end in .json, .yaml or .yml")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8", newline="\n") as f:
                f.write(text)
            print(f"created: {path}")
            return 0
        config = args.config.expanduser()
        _, raw_items = load_config(config)
        items = normalize_items(raw_items)
        shells = SHELLS if args.shell == "all" else (args.shell,)
        builds = []
        for shell in shells:
            path = (args.output or (args.output_dir or Path(".")) / f"prompt.generated.{EXTENSIONS[shell]}").expanduser()
            if path.resolve() == config.resolve():
                raise ConfigError("output must not overwrite the config file")
            builds.append((path, compile_prompt(items, shell, str(config), str(path))))
        if args.check:
            print(f"OK: {config} ({sum(isinstance(x, Segment) for x in items)} segments; {', '.join(shells)})")
            return 0
        # Compile every target before writing, so config errors cannot leave a partial build.
        for path, result in builds:
            atomic_write(path, result)
            print(f"generated: {path}")
        return 0
    except (ConfigError, OSError, UnicodeError, json.JSONDecodeError) as e:
        print(f"promptgen: error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        if yaml is not None and isinstance(e, getattr(yaml, "YAMLError", ())):
            print(f"promptgen: YAML error: {e}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
