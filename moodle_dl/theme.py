"""Flat, minimal design tokens shared by every GUI screen.

Each colour is a (light, dark) pair - CustomTkinter picks the right one for
the system appearance, so the whole window adapts without extra code.
"""
from __future__ import annotations

PAGE = ("#FAFAF8", "#1B1B1A")
CARD = ("#FFFFFF", "#242423")
SUBTLE = ("#F4F3EF", "#2C2C2A")
BORDER = ("#E7E5DF", "#343432")
BORDER_STRONG = ("#D5D2CA", "#43433F")

TEXT = ("#1F1E1D", "#EFEEEB")
TEXT_SECONDARY = ("#6E6D67", "#9C9B95")
TEXT_MUTED = ("#94938C", "#7B7A74")

ACCENT = ("#1F1E1D", "#EFEEEB")          # primary button fill
ACCENT_HOVER = ("#3A3937", "#D4D3CF")
ON_ACCENT = ("#FFFFFF", "#1B1B1A")
GHOST_HOVER = ("#F1EFEA", "#2F2F2D")

SUCCESS = ("#3B9E6E", "#4FB985")
DANGER = ("#C2452F", "#E1705C")

# A failure needs its own surface, not just red text. A grey line in a corner
# reading "something went wrong" was missed even by the person who wrote it.
DANGER_WASH = ("#FDF1EE", "#33211E")
DANGER_BORDER = ("#F0C9BF", "#6A3A31")
DANGER_TEXT = ("#8E2F1F", "#F0A594")

RADIUS_CARD = 12
RADIUS_CTL = 8

FONT = "Segoe UI"
MONO = "Consolas"
