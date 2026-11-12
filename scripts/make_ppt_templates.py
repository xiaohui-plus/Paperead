"""生成内置 PPT 汇报模板（一次性脚本，产物随 sidecar 打包）。

三套 16:9 学术模板，均包含：
  封面页  {{title}} / {{subtitle}} / {{author}} / {{date}}
  章节页  {{title}}
  内容页  {{title}} + 项目符号列表 {{bullets}}
deck.build_deck 会按 {{bullets}} / {{subtitle}} / {{title}} 识别并克隆对应设计页。
"""

from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "server" / "research" / "ppt_templates"
FONT = "Microsoft YaHei"

THEMES = {
    "academic.pptx": {
        "primary": RGBColor(0x12, 0x3B, 0x6D),
        "soft": RGBColor(0xE8, 0xEF, 0xF8),
        "ink": RGBColor(0x1B, 0x22, 0x2C),
        "muted": RGBColor(0x6B, 0x76, 0x86),
        "cover": RGBColor(0x10, 0x33, 0x5E),
        "line": RGBColor(0x2A, 0x5C, 0x9A),
    },
    "seminar.pptx": {
        "primary": RGBColor(0x2F, 0x6B, 0x5A),
        "soft": RGBColor(0xEE, 0xF4, 0xF2),
        "ink": RGBColor(0x22, 0x2A, 0x28),
        "muted": RGBColor(0x6E, 0x7A, 0x76),
        "cover": RGBColor(0x24, 0x55, 0x47),
        "line": RGBColor(0x49, 0x8A, 0x76),
    },
    "defense.pptx": {
        "primary": RGBColor(0x2B, 0x2F, 0x36),
        "soft": RGBColor(0xF3, 0xF4, 0xF5),
        "ink": RGBColor(0x1F, 0x23, 0x28),
        "muted": RGBColor(0x77, 0x7C, 0x84),
        "cover": RGBColor(0x1E, 0x22, 0x27),
        "line": RGBColor(0x8A, 0x90, 0x98),
    },
}

W, H = Inches(13.333), Inches(7.5)


def text(slide, value, x, y, w, h, size, color, *, bold=False, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP):
    box = slide.shapes.add_textbox(x, y, w, h)
    frame = box.text_frame
    frame.word_wrap = True
    frame.vertical_anchor = anchor
    frame.margin_left = 0
    frame.margin_right = 0
    frame.margin_top = 0
    frame.margin_bottom = 0
    paragraph = frame.paragraphs[0]
    paragraph.alignment = align
    run = paragraph.add_run()
    run.text = value
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    run.font.name = FONT
    return box


def rect(slide, x, y, w, h, color):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def blank(prs):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    return slide


def build(path: Path, theme: dict) -> None:
    prs = Presentation()
    prs.slide_width = W
    prs.slide_height = H

    cover = blank(prs)
    cover.background.fill.fore_color.rgb = theme["cover"]
    rect(cover, Inches(0.9), Inches(1.55), Inches(0.55), Inches(0.055), theme["soft"])
    text(cover, "{{title}}", Inches(0.9), Inches(2.05), Inches(11.2), Inches(1.7), 34, RGBColor(0xFF, 0xFF, 0xFF), bold=True)
    text(cover, "{{subtitle}}", Inches(0.9), Inches(3.85), Inches(10.6), Inches(0.9), 16, theme["soft"])
    text(cover, "{{author}}   {{date}}", Inches(0.9), Inches(6.15), Inches(10.6), Inches(0.45), 12, theme["soft"])

    section = blank(prs)
    rect(section, Inches(0), Inches(0), Inches(0.28), H, theme["primary"])
    text(section, "{{title}}", Inches(1.05), Inches(2.75), Inches(10.8), Inches(1.6), 32, theme["primary"], bold=True, anchor=MSO_ANCHOR.MIDDLE)
    rect(section, Inches(1.08), Inches(4.45), Inches(1.1), Inches(0.045), theme["line"])

    body = blank(prs)
    rect(body, Inches(0), Inches(0), W, Inches(0.09), theme["primary"])
    text(body, "{{title}}", Inches(0.9), Inches(0.55), Inches(11.5), Inches(0.95), 24, theme["ink"], bold=True)
    rect(body, Inches(0.92), Inches(1.45), Inches(2.1), Inches(0.035), theme["line"])
    bullets = body.shapes.add_textbox(Inches(0.9), Inches(1.95), Inches(11.5), Inches(4.65))
    frame = bullets.text_frame
    frame.word_wrap = True
    frame.margin_left = 0

    paragraph = frame.paragraphs[0]
    run = paragraph.add_run()
    run.text = "• {{bullets}}"
    run.font.size = Pt(18)
    run.font.color.rgb = theme["ink"]
    run.font.name = FONT
    paragraph.space_after = Pt(10)
    text(body, "{{source}}", Inches(0.9), Inches(6.85), Inches(11.5), Inches(0.4), 10, theme["muted"])

    prs.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, theme in THEMES.items():
        target = OUT / name
        build(target, theme)
        print(f"WROTE {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()