"""把矢量 HTML 流程图导出为 PNG（Chrome headless，两步：先量高度，再截图）。

用法：python export_png.py <源HTML> <输出PNG> [缩放倍数]
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    raise RuntimeError("未找到 Chrome / Edge 浏览器")


def build_export_html(src_html, out_html, card_width, scale):
    """在源 HTML 里注入导出用样式：固定宽度、去掉圆角边框、SVG 按倍数放大。"""
    html = open(src_html, encoding="utf-8").read()

    svg_w = int(card_width * 0.94)          # 与卡片内边距对齐后的绘图区宽度
    svg_h = int(svg_w * 1040 / 680)         # 按原始 viewBox 比例算高

    override = f"""
<style id="export-override">
  html, body {{ background:#ffffff !important; }}
  body {{ display:block !important; padding:0 !important; margin:0 !important; }}
  .card {{ max-width:none !important; width:{card_width}px !important;
           border:none !important; border-radius:0 !important;
           padding:{int(card_width*0.033)}px {int(card_width*0.03)}px !important; }}
  svg {{ width:{svg_w}px !important; height:{svg_h}px !important; }}
</style>
"""
    # 测量脚本：把整页高度写进 title，供第二步读取
    measure = """
<script>
  window.addEventListener('load', function () {
    document.title = 'H=' + Math.ceil(document.body.scrollHeight);
  });
</script>
"""
    html = html.replace("</head>", override + "</head>")
    html = html.replace("</body>", measure + "</body>")
    open(out_html, "w", encoding="utf-8", newline="\n").write(html)
    return svg_w, svg_h


def run(browser, args, timeout=120):
    return subprocess.run([browser] + args, capture_output=True, text=True,
                          timeout=timeout, encoding="utf-8", errors="replace")


def main():
    src = sys.argv[1]
    dst = sys.argv[2]
    scale = float(sys.argv[3]) if len(sys.argv) > 3 else 2

    browser = find_browser()
    card_width = 1440

    # 临时文件放在无中文的路径，避免命令行编码问题
    tmpdir = os.path.join(tempfile.gettempdir(), "fc_export")
    os.makedirs(tmpdir, exist_ok=True)
    tmp_html = os.path.join(tmpdir, "export.html")
    svg_w, svg_h = build_export_html(src, tmp_html, card_width, scale)
    print(f"绘图区: {svg_w} x {svg_h} px")

    url = "file:///" + tmp_html.replace("\\", "/")

    # 第 1 步：量高度
    probe = run(browser, ["--headless=new", "--disable-gpu", "--hide-scrollbars",
                          f"--window-size={card_width},1200", "--virtual-time-budget=3000",
                          "--dump-dom", url])
    m = re.search(r"<title>H=(\d+)</title>", probe.stdout or "")
    if not m:
        raise RuntimeError("测量页面高度失败，Chrome 输出片段：\n" + (probe.stdout or "")[:400]
                           + "\n" + (probe.stderr or "")[:400])
    height = int(m.group(1)) + 4          # 留 4px 余量，避免最后一行被裁
    print(f"页面总高: {height} px")

    # 第 2 步：截图（device scale factor 控制输出分辨率）
    out = os.path.join(tmpdir, "flow.png")
    res = run(browser, ["--headless=new", "--disable-gpu", "--hide-scrollbars",
                        f"--force-device-scale-factor={scale}",
                        f"--window-size={card_width},{height}",
                        "--virtual-time-budget=3000",
                        f"--screenshot={out}", url])
    if not os.path.exists(out):
        raise RuntimeError("截图失败：" + (res.stderr or "")[:400])

    shutil.copyfile(out, dst)
    size = os.path.getsize(dst)
    print(f"已输出: {dst}")
    print(f"文件大小: {size/1024:.0f} KB")


if __name__ == "__main__":
    main()
