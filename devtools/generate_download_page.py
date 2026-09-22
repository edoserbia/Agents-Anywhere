#!/usr/bin/env python3
import html
import pathlib
import re
import sys

version, server_url, output_dir = sys.argv[1:]
root = pathlib.Path(__file__).resolve().parents[1]
notes_dir = root / "docs" / "releases"

def markdown_body(path: pathlib.Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            lines.append(f"<h3>{html.escape(line[3:])}</h3>")
        elif line.startswith("- "):
            text = html.escape(line[2:].strip())
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
            text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
            lines.append(f"<li>{text}</li>")
    return "\n".join(lines)

release_files = sorted(notes_dir.glob("*.md"), key=lambda p: [int(x) for x in re.findall(r"\d+", p.stem)], reverse=True)
current = next((p for p in release_files if p.stem == version), None)
if current is None:
    raise SystemExit(f"missing release notes: {notes_dir / (version + '.md')}")

history = []
for path in release_files:
    release_version = path.stem
    history.append(f'<section class="release"><h2>版本 {html.escape(release_version)}</h2>{markdown_body(path)}</section>')

page = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agents Anywhere 下载</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;max-width:52rem;margin:2.5rem auto;padding:0 1.25rem;line-height:1.6;color:#171717}}
h1{{font-size:1.7rem;margin-bottom:.2rem}} h2{{font-size:1.15rem;margin:0 0 .4rem}} h3{{font-size:.95rem;margin:.8rem 0 .2rem}}
.sub,.note{{color:#666}} .card,.release{{border:1px solid #e5e7eb;border-radius:10px;padding:1rem 1.15rem;margin:1rem 0}}
.current{{border-left:4px solid #10b981}} .card p{{color:#666;font-size:.9rem;margin:.2rem 0 .7rem}}
.btn{{display:inline-block;background:#111827;color:#fff;text-decoration:none;padding:.45rem .85rem;border-radius:7px;font-size:.9rem;font-weight:600;margin:.2rem .35rem .2rem 0}}
code{{background:#f3f4f6;padding:.1rem .3rem;border-radius:4px;font-size:.85em}} li{{margin:.15rem 0}}
@media(prefers-color-scheme:dark){{body{{background:#0b0b0c;color:#f3f4f6}}.card,.release{{border-color:#2a2a2e}}.sub,.note,.card p{{color:#aaa}}.btn{{background:#f3f4f6;color:#111827}}code{{background:#1f1f23}}}}
</style></head><body>
<h1>Agents Anywhere 客户端下载</h1><p class="sub">当前版本 {html.escape(version)} · 服务端同步升级至 {html.escape(version)}</p>
<section class="current"><h2>{html.escape(version)} 本次更新</h2>{markdown_body(current)}</section>
<div class="card"><h2>macOS 桌面客户端（Universal）</h2><p>支持 Apple Silicon 与 Intel。</p><a class="btn" href="Agents%20Anywhere-{version}-universal.dmg" download>下载 DMG</a></div>
<div class="card"><h2>Windows 桌面客户端</h2><p>Windows 安装程序。</p><a class="btn" href="Agents%20Anywhere-{version}-x64.exe" download>下载 EXE</a></div>
<div class="card"><h2>Linux 桌面客户端</h2><p>Linux AppImage。</p><a class="btn" href="Agents%20Anywhere-{version}-x86_64.AppImage" download>下载 AppImage</a></div>
<div class="card"><h2>Android 客户端（APK）</h2><p>可直接安装的 APK。</p><a class="btn" href="agents-anywhere-{version}-debug.apk" download>下载 APK</a></div>
<div class="card"><h2>更新记录</h2><p>查看当前版本和所有历史版本的完整变化。</p><a class="btn" href="CHANGELOG.html">打开 changelog</a> <a class="btn" href="CHANGELOG.md">下载 Markdown</a></div>
<div class="card"><h2>服务端 Web 控制台</h2><a class="btn" href="{html.escape(server_url)}" target="_blank" rel="noreferrer">打开 Web 控制台</a></div>
<p class="note">校验和：<a href="SHA256SUMS.txt">SHA256SUMS.txt</a></p><h2>历史版本</h2>{''.join(history)}</body></html>'''

out = pathlib.Path(output_dir)
out.mkdir(parents=True, exist_ok=True)
(out / "index.html").write_text(page, encoding="utf-8")
changelog = "\n\n".join(path.read_text(encoding="utf-8").strip() for path in release_files) + "\n"
(out / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
changelog_html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agents Anywhere Changelog</title><style>body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;max-width:52rem;margin:2.5rem auto;padding:0 1.25rem;line-height:1.6}}pre{{white-space:pre-wrap;font-family:inherit}}a{{color:#2563eb}}</style></head><body><p><a href="index.html">返回下载页</a></p><h1>Agents Anywhere Changelog</h1><pre>{html.escape(changelog)}</pre></body></html>'''
(out / "CHANGELOG.html").write_text(changelog_html, encoding="utf-8")
