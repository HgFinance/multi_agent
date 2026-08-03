"""Render the 0–7 department architecture as a deterministic PNG."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1600, 1000
ROOT = Path(__file__).parent
OUTPUT = ROOT / "whole_pipeline_0_7.png"
FONT_PATH = "/System/Library/Fonts/Supplemental/Arial.ttf"

NAVY = "#0f172a"
SLATE = "#334155"
MUTED = "#64748b"
PURPLE = "#eef2ff"
PURPLE_DARK = "#818cf8"
HEAD = "#e9d5ff"
WORKER = "#ccfbf1"
TEAL_BORDER = "#14b8a6"
AMBER = "#fef3c7"
AMBER_BORDER = "#d97706"
BLUE = "#e0f2fe"
BLUE_BORDER = "#38bdf8"
GRAY = "#f1f5f9"
GRAY_BORDER = "#94a3b8"
WHITE = "#ffffff"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_PATH if not bold else "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
    return ImageFont.truetype(path, size)


def centered(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, size: int, fill: str = SLATE, bold: bool = False) -> None:
    x1, y1, x2, y2 = box
    fnt = font(size, bold)
    bounds = draw.textbbox((0, 0), text, font=fnt)
    draw.text(((x1 + x2 - (bounds[2] - bounds[0])) / 2, (y1 + y2 - (bounds[3] - bounds[1])) / 2 - 2), text, font=fnt, fill=fill)


def label(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, size: int = 13, fill: str = SLATE, bold: bool = False, anchor: str | None = None) -> None:
    draw.text((x, y), text, font=font(size, bold), fill=fill, anchor=anchor)


def arrow(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], dashed: bool = False, width: int = 3) -> None:
    if not dashed:
        draw.line(points, fill=SLATE, width=width, joint="curve")
    else:
        for start, end in pairwise(points):
            x1, y1 = start
            x2, y2 = end
            distance = max(abs(x2 - x1), abs(y2 - y1))
            steps = max(1, distance // 15)
            for index in range(0, steps, 2):
                a = index / steps
                b = min(1, (index + 1) / steps)
                draw.line((x1 + (x2 - x1) * a, y1 + (y2 - y1) * a, x1 + (x2 - x1) * b, y1 + (y2 - y1) * b), fill=MUTED, width=2)
    x1, y1 = points[-2]
    x2, y2 = points[-1]
    if abs(x2 - x1) >= abs(y2 - y1):
        sign = 1 if x2 > x1 else -1
        tip = (x2, y2)
        left = (x2 - sign * 11, y2 - 7)
        right = (x2 - sign * 11, y2 + 7)
    else:
        sign = 1 if y2 > y1 else -1
        tip = (x2, y2)
        left = (x2 - 7, y2 - sign * 11)
        right = (x2 + 7, y2 - sign * 11)
    draw.polygon((tip, left, right), fill=MUTED if dashed else SLATE)


def dept_box(draw: ImageDraw.ImageDraw, x: int, y: int, number: str, name: str, worker_text: str, worker_detail: str, output: str, *, fill: str = PURPLE, border: str = PURPLE_DARK, gate: str | None = None, dashed: bool = False) -> None:
    draw.rounded_rectangle((x, y, x + 300, y + 205), radius=16, fill=fill, outline=border, width=2)
    if dashed:
        for offset in range(0, 300, 16):
            draw.line((x + offset, y, min(x + offset + 9, x + 300), y), fill=border, width=2)
            draw.line((x + offset, y + 205, min(x + offset + 9, x + 300), y + 205), fill=border, width=2)
    label(draw, x + 15, y + 26, f"{number} {name}", 19, NAVY, True)
    draw.rounded_rectangle((x + 15, y + 42, x + 285, y + 81), radius=8, fill=HEAD)
    label(draw, x + 28, y + 54, "Head: Hermes + Codex/Claude", 14, SLATE, True)
    draw.rounded_rectangle((x + 15, y + 88, x + 285, y + 140), radius=8, fill=WORKER)
    label(draw, x + 28, y + 101, worker_text, 14, SLATE, True)
    label(draw, x + 28, y + 121, worker_detail, 12, MUTED)
    if gate:
        draw.rounded_rectangle((x + 15, y + 148, x + 285, y + 183), radius=8, fill=AMBER, outline="#f59e0b")
        centered(draw, (x + 15, y + 148, x + 285, y + 183), gate, 12, "#92400e", True)
    else:
        label(draw, x + 15, y + 169, output, 12, MUTED)


def lower_box(draw: ImageDraw.ImageDraw, x: int, y: int, number: str, name: str, worker_text: str, worker_detail: str, output: str, *, fill: str = PURPLE, border: str = PURPLE_DARK, dashed: bool = False) -> None:
    draw.rounded_rectangle((x, y, x + 300, y + 175), radius=16, fill=fill, outline=border, width=2)
    if dashed:
        for offset in range(0, 300, 16):
            draw.line((x + offset, y, min(x + offset + 9, x + 300), y), fill=border, width=2)
            draw.line((x + offset, y + 175, min(x + offset + 9, x + 300), y + 175), fill=border, width=2)
    label(draw, x + 15, y + 27, f"{number} {name}", 19, NAVY, True)
    draw.rounded_rectangle((x + 15, y + 43, x + 285, y + 78), radius=8, fill=HEAD)
    label(draw, x + 28, y + 53, "Head: Hermes + Codex/Claude", 14, SLATE, True)
    draw.rounded_rectangle((x + 15, y + 85, x + 285, y + 136), radius=8, fill=WORKER if not dashed else GRAY)
    label(draw, x + 28, y + 98, worker_text, 14, SLATE, True)
    label(draw, x + 28, y + 118, worker_detail, 12, MUTED)
    label(draw, x + 15, y + 151, output, 12, MUTED)


def main() -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(image)

    label(draw, 50, 24, "HgFinance AI Office — 0–7 Department Pipeline", 30, NAVY, True)
    label(draw, 50, 62, "Department Head: Hermes + Codex/Claude Code  |  Employees: independent LangGraph Workers + Ollama qwen3:8b", 16, MUTED)

    draw.rounded_rectangle((1100, 20, 1550, 88), radius=12, fill="#f8fafc", outline="#cbd5e1")
    arrow(draw, [(1120, 44), (1170, 44)])
    label(draw, 1185, 36, "solid = main investment-case flow", 12)
    arrow(draw, [(1120, 70), (1170, 70)], dashed=True)
    label(draw, 1185, 62, "dashed = governance / conditional cycle", 12)

    draw.rounded_rectangle((682, 105, 918, 143), radius=19, fill=AMBER, outline=AMBER_BORDER, width=2)
    centered(draw, (682, 105, 918, 143), "START / case_request", 14, SLATE, True)
    arrow(draw, [(800, 143), (800, 160)])

    dept_box(draw, 40, 160, "1", "Research", "Employees: 6 Workers", "2 always + 4 conditional", "Output: research_packet")
    dept_box(draw, 390, 160, "2", "Trading", "Employees: 6 Workers", "2 always + 4 conditional", "Output: order_intent")
    dept_box(draw, 740, 160, "3", "Risk Management", "Employees: 4 Workers", "2 always + 2 conditional", "", gate="DETERMINISTIC RISK GATE")
    dept_box(draw, 1090, 160, "6", "AI QA / Audit", "Employees: 5 Workers", "1 always + 4 conditional", "", gate="DETERMINISTIC EVIDENCE QA GATE")

    arrow(draw, [(340, 260), (390, 260)])
    label(draw, 365, 238, "research_packet", 12, "#1d4ed8", True, "mm")
    arrow(draw, [(690, 260), (740, 260)])
    label(draw, 715, 238, "order_intent", 12, "#1d4ed8", True, "mm")
    arrow(draw, [(1040, 260), (1090, 260)])
    label(draw, 1065, 238, "risk_decision", 12, "#1d4ed8", True, "mm")

    lower_box(draw, 40, 505, "4", "Quant / Backtest", "Employees: 7 Workers", "2 always + 5 conditional", "strategy_research_cycle", fill=BLUE, border=BLUE_BORDER)
    lower_box(draw, 390, 505, "7", "HR / Agent Workforce", "Employees: 5 Workers", "hire / evaluate / pause / retire", "shared governance service", fill=GRAY, border=GRAY_BORDER, dashed=True)
    lower_box(draw, 740, 505, "5", "Accounting / Portfolio", "Employees: 8 Workers", "2 always + 6 conditional", "Output: accounting_snapshot")
    lower_box(draw, 1090, 505, "0", "CEO Office", "Employees: 1 Worker", "briefing context only", "Final summary; no order authority", fill=AMBER, border=AMBER_BORDER)

    arrow(draw, [(1240, 365), (1240, 420), (900, 420), (900, 505)])
    label(draw, 1080, 404, "qa_assessment", 12, "#1d4ed8", True, "mm")
    arrow(draw, [(1040, 592), (1090, 592)])
    label(draw, 1065, 570, "accounting_snapshot", 12, "#1d4ed8", True, "mm")
    arrow(draw, [(1240, 680), (1240, 720)])
    draw.rounded_rectangle((1090, 725, 1390, 763), radius=19, fill=AMBER, outline=AMBER_BORDER, width=2)
    centered(draw, (1090, 725, 1390, 763), "END / ceo_case_summary", 14, SLATE, True)

    arrow(draw, [(190, 505), (190, 430), (540, 430), (540, 365)], dashed=True)
    label(draw, 350, 414, "strategy_bundle", 12, "#1d4ed8", True, "mm")
    arrow(draw, [(325, 590), (520, 470), (970, 470), (1105, 320)], dashed=True)
    label(draw, 690, 452, "quant evidence → Trading / QA", 12, MUTED, False, "mm")
    arrow(draw, [(540, 505), (540, 455), (180, 455), (180, 365)], dashed=True)
    arrow(draw, [(540, 505), (540, 455), (890, 455), (890, 365)], dashed=True)
    arrow(draw, [(675, 590), (840, 470), (1200, 470), (1240, 365)], dashed=True)
    label(draw, 520, 485, "HR lifecycle: hire / evaluate / pause / retire / profile change", 12, MUTED, False, "mm")

    draw.rounded_rectangle((40, 790, 1390, 915), radius=14, fill="#f8fafc", outline="#cbd5e1")
    label(draw, 65, 812, "Shared contracts", 14, SLATE, True)
    label(draw, 65, 840, "case_request → research_packet → order_intent → risk_decision → qa_assessment → execution_result", 13)
    label(draw, 65, 865, "→ accounting_snapshot → ceo_case_summary", 13)
    label(draw, 65, 890, "Every Worker emits non-binding worker-context.v1. Risk / Evidence QA gates own binding safety decisions.", 13)
    label(draw, 65, 905, "Paper mode has no broker, OMS, ledger, or production side effects. Live mode requires approved adapters and credentials.", 13)

    draw.rounded_rectangle((1420, 790, 1550, 915), radius=14, fill="#fff7ed", outline="#fdba74")
    label(draw, 1435, 812, "LEGEND", 12, "#92400e", True)
    label(draw, 1435, 840, "purple: Head", 12)
    label(draw, 1435, 862, "teal: Worker", 12)
    label(draw, 1435, 884, "amber: Gate", 12)
    label(draw, 1435, 906, "blue: Data", 12)

    image.save(OUTPUT, format="PNG", optimize=True)
    print(OUTPUT)


if __name__ == "__main__":
    main()
