"""样式：常量字符串，页面构建时放进 <head>（不含任何内核数据）。亮色与暗色（Quasar 的 body--dark）各一套。"""
from __future__ import annotations

TONES = ("ok", "info", "warn", "danger", "unknown")

CSS = """
.dmr-header { background: #1f2933; color: #fff; padding: 0; }
.dmr-topbar { padding: 6px 16px; gap: 14px; }
.dmr-title { font-size: 18px; font-weight: 700; letter-spacing: .5px; }
.dmr-nav { color: #cbd5e1 !important; text-decoration: none; padding: 4px 10px; border-radius: 4px; }
.dmr-nav:hover { background: rgba(255,255,255,.12); }
.dmr-nav-active { color: #fff !important; background: rgba(255,255,255,.2); font-weight: 600; }
.dmr-operator { color: #cbd5e1; font-size: 13px; }
.dmr-banners { width: 100%; }
.dmr-banner { width: 100%; padding: 8px 16px; }
.dmr-banner-text { font-size: 17px; font-weight: 800; letter-spacing: 1px; }
.dmr-banner-detail { font-size: 12px; opacity: .85; }
.dmr-banner-warn { background: repeating-linear-gradient(135deg, #ffd54f 0 18px, #ffca28 18px 36px); color: #3e2c00; border-bottom: 3px solid #b28704; }
.dmr-banner-danger { background: #c62828; color: #fff; border-bottom: 3px solid #7f0000; }
.dmr-main { padding: 12px 16px 32px; gap: 12px; max-width: 1800px; }
.dmr-chips { display: grid; grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); gap: 6px; width: 100%; }
.dmr-chip { border: 1px solid; border-radius: 6px; padding: 5px 10px; min-width: 0; }
.dmr-chip-label { font-size: 11px; opacity: .75; }
.dmr-chip-value { font-size: 14px; font-weight: 600; overflow-wrap: anywhere; }
.dmr-updated { font-size: 12px; opacity: .7; }
.dmr-tone-ok { background: #e8f5e9; border-color: #81c784; color: #1b5e20; }
.dmr-tone-info { background: #e3f2fd; border-color: #90caf9; color: #0d47a1; }
.dmr-tone-warn { background: #fff8e1; border-color: #ffca28; color: #7a5200; }
.dmr-tone-danger { background: #ffebee; border-color: #e53935; color: #b71c1c; }
.dmr-tone-unknown { background: #eceff1; border-color: #b0bec5; color: #455a64; }
.dmr-h2 { font-size: 16px; font-weight: 700; }
.dmr-hint, .dmr-empty, .dmr-meaning, .dmr-reason { font-size: 12px; opacity: .8; }
.dmr-readonly { font-size: 13px; padding: 2px 10px; border-radius: 4px; background: #fff8e1; color: #7a5200; border: 1px solid #ffca28; }
.dmr-part-errors { font-size: 13px; color: #b71c1c; }
.dmr-ops { display: flex; flex-wrap: wrap; gap: 12px; }
.dmr-op { min-width: 210px; flex: 1 1 210px; gap: 6px; }
.dmr-dialog { min-width: min(560px, 94vw); max-width: 94vw; gap: 8px; }
.dmr-refusal { color: #c62828; font-weight: 600; font-size: 14px; opacity: 1; }
.dmr-msg { font-size: 13px; min-height: 18px; white-space: pre-line; }
.dmr-standin-tag { display: inline-block; font-size: 20px; font-weight: 800; letter-spacing: 1px; padding: 6px 14px; border-radius: 6px; background: #ffca28; color: #3e2c00; border: 3px solid #b28704; }
.dmr-login { width: min(420px, 92vw); }
.dmr-login-error { color: #b71c1c; font-size: 14px; min-height: 20px; }
.dmr-neg { color: #c62828; font-weight: 600; }
.dmr-pos { color: #2e7d32; font-weight: 600; }
.dmr-warn-text { color: #b26a00; font-weight: 600; }
.dmr-big-row { display: flex; flex-wrap: wrap; gap: 16px; width: 100%; }
.dmr-big { min-width: 200px; flex: 1 1 200px; border: 1px solid #b0bec5; border-radius: 8px; padding: 12px 16px; gap: 2px; }
.dmr-big-label { font-size: 14px; opacity: .8; }
.dmr-big-value { font-size: 40px; font-weight: 800; line-height: 1.1; }
.dmr-detail-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 6px; width: 100%; }
.dmr-detail { border: 1px solid #cfd8dc; border-radius: 6px; padding: 5px 10px; gap: 0; }
.dmr-detail-value { font-size: 16px; font-weight: 600; }
.m3-opt { border: 1px solid #b0bec5; border-radius: 12px; padding: 2px 12px; font-size: 13px; opacity: .75; }
.m3-opt-current { background: #1f2933; color: #fff; border-color: #1f2933; font-weight: 700; opacity: 1; }
.ag-row.dmr-row-danger { background-color: rgba(229,57,53,.16) !important; }
.ag-row.dmr-row-warn { background-color: rgba(255,193,7,.18) !important; }
body.body--dark .dmr-tone-ok { background: #17301a; border-color: #388e3c; color: #a5d6a7; }
body.body--dark .dmr-tone-info { background: #0e2438; border-color: #1976d2; color: #90caf9; }
body.body--dark .dmr-tone-warn { background: #33280a; border-color: #b28704; color: #ffe082; }
body.body--dark .dmr-tone-danger { background: #3b1114; border-color: #e53935; color: #ef9a9a; }
body.body--dark .dmr-tone-unknown { background: #263238; border-color: #546e7a; color: #b0bec5; }
body.body--dark .dmr-readonly { background: #33280a; color: #ffe082; border-color: #b28704; }
body.body--dark .dmr-part-errors, body.body--dark .dmr-login-error { color: #ef9a9a; }
body.body--dark .dmr-neg { color: #ef9a9a; }
body.body--dark .dmr-big, body.body--dark .dmr-detail, body.body--dark .m3-opt { border-color: #546e7a; }
body.body--dark .m3-opt-current { background: #eceff1; color: #1f2933; border-color: #eceff1; }
body.body--dark .dmr-pos { color: #a5d6a7; }
"""

STYLE_TAG = f"<style>{CSS}</style>"


def tone_class(tone: str) -> str:
    return f"dmr-tone-{tone if tone in TONES else 'unknown'}"
