"""Stitch a header + the four code files into one Markdown deliverable.
usage: python build_md.py <header.md> <out.md> [<code section title>]"""
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
header = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "KIT_HEADER.md")
out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(here, "..", "RESEARCH_KIT.md")
title = sys.argv[3] if len(sys.argv) > 3 else "## 8. The code"
parts = [open(header).read().rstrip(), "", title, ""]
for fname, lang, note in [
    ("requirements.txt", "text", "Python dependencies."),
    ("config.py", "python", "Pre-filled for your files: check the three paths at the top."),
    ("research.py", "python", "All stages (v2 is a superset of v1). Do not edit."),
    ("make_synthetic.py", "python", "Optional: fake data in either layout, for the smoke test."),
]:
    code = open(os.path.join(here, fname)).read().rstrip() + "\n"
    parts += [f"### `{fname}`", "", note, "", f"```{lang}", code + "```", ""]
with open(out, "w") as fh:
    fh.write("\n".join(parts))
print(f"wrote {os.path.abspath(out)} ({os.path.getsize(out)/1024:.0f} KB)")
