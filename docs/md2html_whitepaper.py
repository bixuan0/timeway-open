# -*- coding: utf-8 -*-
"""
md2html_whitepaper.py — 白皮书 md → 自包含 HTML（评委可直接打开的单文件）
===========================================================================
不依赖外部 md 库：针对本白皮书实际用到的语法子集手写转换
（标题层级 / 表格 / fenced 代码块 / 无序列表 / 引用块 / 粗体 / 行内代码），
输出单文件 HTML：CSS 内嵌、目录锚点、§19 场景语言层高亮、深色代码块。

用法（venv python）：
  python md2html_whitepaper.py <in.md> <out.html>
"""
from __future__ import annotations

import os
import re
import sys

CSS = """
:root{--bg:#ffffff;--ink:#1f2937;--muted:#6b7280;--blue:#1e88e5;--red:#ef4444;--green:#1e7e34;--code-bg:#1f2937;--code-ink:#e5e7eb;--line:#e5e7eb;--hl:#fff7e6;}
*{box-sizing:border-box}
body{font-family:-apple-system,"Microsoft YaHei","PingFang SC",sans-serif;color:var(--ink);background:var(--bg);max-width:880px;margin:0 auto;padding:32px 20px 80px;line-height:1.65;font-size:15px}
h1{font-size:26px;border-bottom:3px solid var(--blue);padding-bottom:10px}
h2{font-size:20px;color:var(--blue);border-left:4px solid var(--blue);padding-left:10px;margin-top:36px}
h3{font-size:16px;margin-top:26px}
blockquote{border-left:4px solid var(--blue);background:#f0f7ff;margin:14px 0;padding:10px 16px;color:#334155}
table{border-collapse:collapse;width:100%;font-size:13px;margin:14px 0}
th{background:var(--blue);color:#fff;padding:6px 8px;text-align:left;border:1px solid #cbd5e1}
td{border:1px solid var(--line);padding:6px 8px;vertical-align:top}
tr:nth-child(even){background:#f6f9fc}
code.inline{background:#eef2f7;padding:1px 6px;border-radius:4px;font-family:Consolas,monospace;font-size:13px}
pre{background:var(--code-bg);color:var(--code-ink);padding:14px 16px;border-radius:6px;overflow-x:auto;font-size:13px;line-height:1.5}
pre code{font-family:Consolas,monospace;background:none;color:inherit;padding:0}
ul{padding-left:22px}
li{margin:4px 0}
strong{font-weight:700}
.scene-lang{background:linear-gradient(180deg,#fff8ec,#fff3d6);border:1px solid #f0c15a;border-radius:8px;padding:14px 18px;margin:18px 0}
.scene-lang h2,.scene-lang h3,.scene-lang h1{color:#b45309;border-color:#f0c15a}
a{color:var(--blue);text-decoration:none}
.toc{background:#f8fafc;border:1px solid var(--line);border-radius:8px;padding:12px 18px;font-size:14px}
.toc a{display:block;padding:2px 0}
.toc h4{margin:4px 0;color:var(--muted)}
.toc .l1{padding-left:14px;font-weight:600}
hr{border:none;border-top:1px solid var(--line);margin:28px 0}
"""


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def inline(s: str) -> str:
    """行内：`code` → <code class=inline>；**bold** → <strong>。"""
    s = esc(s)
    s = re.sub(r"`([^`]+)`", r'<code class="inline">\1</code>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s


def convert(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    in_code = False
    in_list = False
    while i < n:
        ln = lines[i]
        stripped = ln.strip()

        # fenced code block
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                out.append("<pre><code>")
                i += 1
                continue
            in_code = False
            out.append("</code></pre>")
            i += 1
            continue
        if in_code:
            out.append(esc(ln))
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = len(m.group(1))
            title = inline(m.group(2))
            anchor = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", m.group(2))
            anchor = anchor.strip("-")
            out.append(f'<h{level} id="{anchor}">{title}</h{level}>')
            i += 1
            continue

        # 表格（连续 | 行）
        if stripped.startswith("|") and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            # 表头行 + 分隔行
            head = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            html = ["<table>", "<thead><tr>"]
            html += [f"<th>{inline(c)}</th>" for c in head]
            html += ["</tr></thead><tbody>"]
            for r in rows:
                html.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            html.append("</tbody></table>")
            out.append("".join(html))
            continue

        # 引用块
        if stripped.startswith(">"):
            out.append(f"<blockquote>{inline(stripped.lstrip('>').strip())}</blockquote>")
            i += 1
            continue

        # 无序列表（连续项）
        if re.match(r"^\s*[-*]\s+", ln):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(re.sub(r'^\\s*[-*]\\s+', '', ln))}</li>")
            i += 1
            continue
        if in_list:
            out.append("</ul>")
            in_list = False

        # 分隔线
        if re.match(r"^\s*---+\s*$", ln):
            out.append("<hr>")
            i += 1
            continue

        # 段落
        if stripped:
            out.append(f"<p>{inline(stripped)}</p>")
        i += 1
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def toc_from(md: str) -> str:
    """提取 ## / ### 标题生成目录。"""
    items = []
    for ln in md.split("\n"):
        m = re.match(r"^(#{2,3})\s+(.*)$", ln.strip())
        if not m:
            continue
        level, title = len(m.group(1)), m.group(2)
        anchor = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title).strip("-")
        cls = "" if level == 2 else "l1"
        items.append(f'<a class="{cls}" href="#{anchor}">{esc(title)}</a>')
    if not items:
        return ""
    return '<div class="toc"><h4>目录</h4>' + "".join(items) + "</div>"


def main():
    if len(sys.argv) < 3:
        print("用法: python md2html_whitepaper.py <in.md> <out.html>")
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as f:
        md = f.read()
    body = convert(md)
    toc = toc_from(md)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TimeWay 算法白皮书（含 §19 SceneLang）</title>
<style>{CSS}</style></head>
<body>
{toc}
{body}
<hr>
<p style="color:var(--muted);font-size:13px">
本文档由 <code class="inline">docs/TimeWay_算法白皮书.md</code> 自动转换生成，
以源码 md 为最终依据。</p>
</body></html>"""
    with open(dst, "w", encoding="utf-8") as f:
        f.write(html)
    print("HTML 已生成:", dst, f"({len(html)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())