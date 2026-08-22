#!/usr/bin/env python3
"""Build a self-contained AIHOT 30-day editorial snapshot."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OUTPUT = ROOT / "aihot-hotspots.html"


def read_json(name: str) -> dict:
    with (DATA_DIR / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build() -> None:
    daily = read_json("aihot-daily-index.json")
    hotspots = read_json("aihot-hot-topics.json")
    recent = read_json("aihot-recent-items.json")
    recent_items = recent.get("items", [])
    scores = [item.get("score") for item in recent_items if isinstance(item.get("score"), (int, float))]

    snapshot = {
        "snapshotDate": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d"),
        "window": {
            "from": daily.get("items", [])[-1].get("date") if daily.get("items") else None,
            "to": daily.get("items", [])[0].get("date") if daily.get("items") else None,
        },
        "daily": daily.get("items", []),
        "hotspots": hotspots.get("items", []),
        "recent": recent_items,
        "metrics": {
            "dailyCount": len(daily.get("items", [])),
            "hotspotCount": len(hotspots.get("items", [])),
            "recentCount": len(recent_items),
            "averageScore": round(sum(scores) / len(scores), 1) if scores else None,
        },
    }
    embedded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.replace("__SNAPSHOT_DATA__", embedded)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"built {OUTPUT} ({len(html):,} bytes)")


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="AIHOT 最近 30 天 AI 新闻热点与当前热点雷达">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23060812'/%3E%3Cpath d='M12 42L28 12l8 14 16-6-16 32-8-14z' fill='%238af4ff'/%3E%3C/svg%3E">
  <title>AIHOT / 30 天热点信号</title>
  <style>
    :root {
      --ink: #f6f7f9;
      --muted: #9ea6b8;
      --faint: #606a7d;
      --night: #060812;
      --panel: #0b1020;
      --line: rgba(229, 236, 255, .14);
      --line-strong: rgba(229, 236, 255, .28);
      --cyan: #8af4ff;
      --blue: #385eff;
      --pink: #ff3ca8;
      --acid: #e6f56f;
      --mx: 50%;
      --my: 42%;
      --scroll-y: 0px;
    }

    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; background: var(--night); }
    body {
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at 88% 12%, rgba(56, 94, 255, .16), transparent 28rem),
        linear-gradient(180deg, #060812 0%, #080c18 46%, #05070e 100%);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.55;
      overflow-x: hidden;
    }

    body::before {
      position: fixed;
      inset: 0;
      z-index: -1;
      pointer-events: none;
      content: "";
      opacity: .12;
      background-image: linear-gradient(rgba(255, 255, 255, .03) 1px, transparent 1px), linear-gradient(90deg, rgba(255, 255, 255, .03) 1px, transparent 1px);
      background-size: 44px 44px;
      mask-image: linear-gradient(to bottom, black, transparent 72%);
    }

    a { color: inherit; }
    button { font: inherit; }
    ::selection { color: var(--night); background: var(--cyan); }

    .progress {
      position: fixed;
      inset: 0 0 auto;
      z-index: 20;
      height: 3px;
      transform-origin: left;
      background: linear-gradient(90deg, var(--cyan), var(--blue), var(--pink));
      transform: scaleX(0);
    }

    .site-nav {
      position: absolute;
      inset: 0 0 auto;
      z-index: 10;
      display: flex;
      align-items: center;
      justify-content: space-between;
      width: min(1240px, calc(100% - 48px));
      margin: 0 auto;
      padding: 28px 0;
    }

    .wordmark {
      display: inline-flex;
      align-items: baseline;
      gap: 12px;
      color: var(--ink);
      text-decoration: none;
      letter-spacing: -.04em;
    }

    .wordmark strong { font-size: 1.08rem; letter-spacing: .02em; }
    .wordmark small, .nav-link, .eyebrow, .micro-label, .meta-label {
      color: var(--muted);
      font-size: .65rem;
      font-weight: 700;
      letter-spacing: .16em;
      text-transform: uppercase;
    }

    .wordmark small { color: var(--cyan); }
    .nav-links { display: flex; gap: 26px; }
    .nav-link { color: #d2d7e4; text-decoration: none; transition: color .25s ease; }
    .nav-link:hover { color: var(--cyan); }

    .hero {
      position: relative;
      display: flex;
      align-items: flex-end;
      min-height: 93svh;
      overflow: hidden;
      isolation: isolate;
      padding: 160px max(24px, calc((100% - 1240px) / 2)) 56px;
      background: radial-gradient(circle at var(--mx) var(--my), rgba(138, 244, 255, .1), transparent 25rem);
    }

    .hero-art {
      position: absolute;
      inset: -5% -2% -4%;
      z-index: -2;
      background: url("assets/punk-collage-dark.png") center / cover no-repeat;
      opacity: .72;
      transform: translateY(calc(var(--scroll-y) * -.07)) scale(1.04);
      filter: saturate(.9) contrast(1.08);
      will-change: transform;
    }

    .hero::before {
      position: absolute;
      inset: 0;
      z-index: -1;
      content: "";
      background: linear-gradient(90deg, rgba(5, 7, 15, .96) 0%, rgba(5, 7, 15, .68) 38%, rgba(5, 7, 15, .25) 100%), linear-gradient(0deg, #060812 0%, transparent 40%, rgba(6, 8, 18, .12) 100%);
    }

    .hero::after {
      position: absolute;
      inset: 0;
      z-index: -1;
      content: "";
      pointer-events: none;
      background: linear-gradient(120deg, transparent 0 48%, rgba(138, 244, 255, .06) 48.1% 48.25%, transparent 48.35% 100%);
      mix-blend-mode: screen;
    }

    .hero-copy { width: min(810px, 100%); }
    .eyebrow { display: flex; align-items: center; gap: 10px; color: var(--cyan); animation: rise .7s both; }
    .eyebrow::before { width: 34px; height: 1px; content: ""; background: var(--cyan); box-shadow: 0 0 14px var(--cyan); }
    .hero h1 {
      max-width: 840px;
      margin: 20px 0 24px;
      font-size: clamp(3.6rem, 10vw, 8.8rem);
      font-weight: 780;
      line-height: .9;
      letter-spacing: -.085em;
      animation: rise .75s .08s both;
    }
    .hero h1 span { color: var(--cyan); text-shadow: 0 0 30px rgba(138, 244, 255, .22); }
    .hero h1 em { display: block; color: var(--pink); font-style: normal; }
    .hero-summary {
      max-width: 600px;
      margin: 0;
      color: #c6ccda;
      font-size: clamp(1rem, 1.5vw, 1.2rem);
      animation: rise .75s .16s both;
    }
    .hero-summary b { color: var(--ink); font-weight: 700; }

    .hero-rail {
      display: flex;
      align-items: center;
      gap: 18px;
      margin-top: 52px;
      color: var(--muted);
      font-size: .76rem;
      animation: rise .75s .24s both;
    }
    .hero-rail a { color: var(--cyan); text-underline-offset: 4px; }
    .live-dot { width: 8px; height: 8px; border-radius: 999px; background: var(--acid); box-shadow: 0 0 0 6px rgba(230, 245, 111, .1), 0 0 18px var(--acid); animation: pulse 1.8s infinite; }
    .scroll-cue { margin-left: auto; display: inline-flex; align-items: center; gap: 12px; color: var(--muted); }
    .scroll-cue i { display: block; width: 50px; height: 1px; background: var(--line-strong); }

    .section { width: min(1240px, calc(100% - 48px)); margin: 0 auto; padding: 120px 0; }
    .section--tight { padding-top: 72px; }
    .section-head { display: flex; align-items: end; justify-content: space-between; gap: 24px; margin-bottom: 42px; }
    .section-head h2 { max-width: 680px; margin: 0; font-size: clamp(2.1rem, 4vw, 4.7rem); line-height: .95; letter-spacing: -.07em; }
    .section-head p { max-width: 360px; margin: 0; color: var(--muted); font-size: .9rem; }
    .section-index { color: var(--cyan); font-size: .7rem; font-weight: 700; letter-spacing: .2em; }

    .metrics { display: grid; grid-template-columns: repeat(4, 1fr); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); margin-top: 72px; }
    .metric { padding: 24px 18px 26px 0; border-right: 1px solid var(--line); }
    .metric + .metric { padding-left: 24px; }
    .metric:last-child { border-right: 0; }
    .metric-number { display: block; color: var(--ink); font-size: clamp(2rem, 4vw, 3.6rem); font-weight: 780; line-height: 1; letter-spacing: -.08em; }
    .metric-label { display: block; margin-top: 8px; color: var(--muted); font-size: .75rem; }

    .hotspot-list { border-top: 1px solid var(--line-strong); }
    .hotspot-item { display: grid; grid-template-columns: 86px minmax(0, 1fr) minmax(220px, .32fr) 120px; gap: 24px; align-items: center; min-height: 150px; border-bottom: 1px solid var(--line); transition: background .3s ease, padding .3s ease; }
    .hotspot-item:hover { padding: 0 18px; background: linear-gradient(90deg, rgba(138, 244, 255, .07), transparent 80%); }
    .hotspot-rank { color: var(--faint); font-size: 3rem; font-weight: 760; line-height: 1; letter-spacing: -.1em; }
    .hotspot-item:first-child .hotspot-rank { color: var(--acid); }
    .hotspot-title { margin: 0; font-size: clamp(1.12rem, 2vw, 1.65rem); line-height: 1.15; letter-spacing: -.045em; }
    .hotspot-source { margin: 9px 0 0; color: var(--muted); font-size: .75rem; }
    .hotspot-sources { color: #cdd4e0; font-size: .76rem; line-height: 1.6; }
    .hotspot-sources strong { display: block; margin-bottom: 6px; color: var(--cyan); font-size: .65rem; letter-spacing: .12em; text-transform: uppercase; }
    .signal-meta { justify-self: end; text-align: right; color: var(--muted); font-size: .72rem; }
    .signal-meta strong { display: block; color: var(--ink); font-size: 1.15rem; }
    .signal-meta a { display: inline-block; margin-top: 8px; color: var(--cyan); text-decoration: none; }

    .pulse-panel { position: relative; overflow: hidden; border-top: 1px solid var(--line-strong); border-bottom: 1px solid var(--line); padding: 12px 0 0; }
    .pulse-panel::before { position: absolute; inset: 0; pointer-events: none; content: ""; background: linear-gradient(90deg, transparent, rgba(56, 94, 255, .06), transparent); transform: translateX(-100%); animation: sweep 8s ease-in-out infinite; }
    .timeline { position: relative; }
    .timeline::before { position: absolute; left: 158px; top: 0; bottom: 0; width: 1px; content: ""; background: linear-gradient(var(--cyan), rgba(138, 244, 255, .08)); }
    .timeline-item { position: relative; display: grid; grid-template-columns: 138px 42px minmax(0, 1fr) 140px; gap: 20px; align-items: center; min-height: 76px; border-bottom: 1px solid rgba(229, 236, 255, .08); opacity: 0; transform: translateY(12px); animation: rise .65s calc(var(--i) * 30ms) forwards; }
    .timeline-date { color: var(--muted); font-size: .78rem; font-variant-numeric: tabular-nums; }
    .timeline-dot { position: relative; z-index: 1; width: 9px; height: 9px; border: 2px solid var(--cyan); border-radius: 50%; background: var(--night); box-shadow: 0 0 0 5px rgba(138, 244, 255, .08); transition: transform .25s ease, background .25s ease; }
    .timeline-item:hover .timeline-dot { background: var(--cyan); transform: scale(1.5); }
    .timeline-title { margin: 0; font-size: .95rem; font-weight: 620; line-height: 1.35; }
    .timeline-link { justify-self: end; color: var(--faint); font-size: .68rem; text-decoration: none; }
    .timeline-link:hover { color: var(--cyan); }

    .feed-toolbar { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 30px; }
    .filter { cursor: pointer; border: 1px solid var(--line); border-radius: 999px; padding: 9px 14px; color: var(--muted); background: transparent; font-size: .75rem; transition: all .22s ease; }
    .filter:hover, .filter[aria-pressed="true"] { border-color: var(--cyan); color: var(--night); background: var(--cyan); }
    .feed-list { border-top: 1px solid var(--line-strong); }
    .news-row { display: grid; grid-template-columns: 110px minmax(0, 1fr) 90px; gap: 26px; padding: 28px 0 30px; border-bottom: 1px solid var(--line); }
    .news-kicker { color: var(--cyan); font-size: .68rem; font-weight: 750; letter-spacing: .08em; text-transform: uppercase; }
    .news-date { margin-top: 8px; color: var(--muted); font-size: .72rem; }
    .news-row h3 { margin: 0; font-size: clamp(1.05rem, 1.7vw, 1.45rem); line-height: 1.2; letter-spacing: -.04em; }
    .news-row p { margin: 10px 0 0; color: #b4bdcd; font-size: .85rem; line-height: 1.7; }
    .news-reason { margin-top: 14px !important; color: var(--muted) !important; font-size: .78rem !important; }
    .news-reason::before { margin-right: 8px; color: var(--acid); content: "↳"; }
    .news-score { justify-self: end; text-align: right; color: var(--muted); font-size: .68rem; }
    .news-score strong { display: block; color: var(--ink); font-size: 2.1rem; line-height: 1; letter-spacing: -.08em; }
    .news-links { display: flex; gap: 12px; margin-top: 16px; }
    .news-links a { color: var(--cyan); font-size: .72rem; text-decoration: none; text-underline-offset: 4px; }
    .news-links a:hover { text-decoration: underline; }

    .method { display: grid; grid-template-columns: .7fr 1.3fr; gap: 80px; padding: 96px 0 120px; border-top: 1px solid var(--line); }
    .method h2 { margin: 0; font-size: clamp(2rem, 4vw, 4rem); line-height: .95; letter-spacing: -.07em; }
    .method-copy { color: var(--muted); font-size: .88rem; }
    .method-copy p { max-width: 680px; margin: 0 0 18px; }
    .source-note { padding: 18px 0 0; border-top: 1px solid var(--line); color: #d6dbe6; font-size: .76rem; }
    .source-note a { color: var(--cyan); }

    .site-footer { display: flex; justify-content: space-between; gap: 24px; width: min(1240px, calc(100% - 48px)); margin: 0 auto; padding: 24px 0 38px; border-top: 1px solid var(--line); color: var(--faint); font-size: .68rem; }
    .site-footer a { color: var(--muted); }

    .reveal { opacity: 0; transform: translateY(24px); transition: opacity .8s ease, transform .8s ease; }
    .reveal.is-visible { opacity: 1; transform: none; }
    .empty { padding: 36px 0; color: var(--muted); }

    @keyframes rise { from { opacity: 0; transform: translateY(18px); } to { opacity: 1; transform: none; } }
    @keyframes pulse { 0%, 100% { opacity: .8; transform: scale(.9); } 50% { opacity: 1; transform: scale(1.1); } }
    @keyframes sweep { 0%, 55% { transform: translateX(-100%); } 75%, 100% { transform: translateX(100%); } }

    @media (max-width: 820px) {
      .site-nav, .section, .site-footer { width: min(100% - 32px, 680px); }
      .site-nav { padding-top: 20px; }
      .nav-links { gap: 14px; }
      .nav-link { font-size: .58rem; letter-spacing: .08em; }
      .nav-link:not(:first-child) { display: none; }
      .hero { min-height: 88svh; padding: 142px 16px 34px; }
      .hero-art { background-position: 48% center; }
      .hero h1 { max-width: 100%; font-size: clamp(3.15rem, 16.5vw, 6rem); letter-spacing: -.1em; }
      .hero-rail { flex-wrap: wrap; margin-top: 34px; }
      .scroll-cue { width: 100%; margin-left: 0; }
      .metrics { grid-template-columns: repeat(2, 1fr); margin-top: 52px; }
      .metric:nth-child(2) { border-right: 0; }
      .metric:nth-child(3) { padding-left: 0; border-top: 1px solid var(--line); }
      .metric:nth-child(4) { border-top: 1px solid var(--line); }
      .section { padding: 80px 0; }
      .section-head { display: block; margin-bottom: 28px; }
      .section-head p { margin-top: 18px; }
      .hotspot-item { grid-template-columns: 54px minmax(0, 1fr); gap: 12px; padding: 20px 0; }
      .hotspot-item:hover { padding: 20px 10px; }
      .hotspot-rank { font-size: 2.3rem; }
      .hotspot-sources, .signal-meta { grid-column: 2; justify-self: start; text-align: left; }
      .signal-meta strong { display: inline; margin-right: 6px; }
      .timeline::before { left: 83px; }
      .timeline-item { grid-template-columns: 70px 26px minmax(0, 1fr); gap: 13px; min-height: 92px; }
      .timeline-link { display: none; }
      .news-row { grid-template-columns: 1fr 60px; gap: 12px; }
      .news-meta { grid-column: 1 / -1; display: flex; align-items: baseline; gap: 12px; }
      .news-date { margin-top: 0; }
      .news-content { grid-column: 1; }
      .news-score { grid-column: 2; grid-row: 2; }
      .method { display: block; padding: 72px 0; }
      .method-copy { margin-top: 28px; }
      .site-footer { display: block; }
      .site-footer span { display: block; margin-top: 8px; }
    }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
      .hero-art { transform: none; }
    }
  </style>
</head>
<body>
  <div class="progress" id="progress" aria-hidden="true"></div>
  <header class="site-nav">
    <a class="wordmark" href="#top" aria-label="返回顶部"><strong>AIHOT</strong><small>EDITORIAL SIGNALS</small></a>
    <nav class="nav-links" aria-label="页面导航">
      <a class="nav-link" href="#radar">热点雷达</a>
      <a class="nav-link" href="#pulse">30 天脉冲</a>
      <a class="nav-link" href="#feed">精选流</a>
    </nav>
  </header>

  <main>
    <section class="hero" id="top">
      <div class="hero-art" aria-hidden="true"></div>
      <div class="hero-copy">
        <div class="eyebrow">AI INDUSTRY / LOCAL SNAPSHOT</div>
        <h1>最近 30 天<br><span>AI</span><em>热点信号</em></h1>
        <p class="hero-summary">从模型、Agent、基础设施到机器人，<b>把 AIHOT 的每日精选压成一张能扫懂的编辑台。</b> 当前快照覆盖 2026-07-24 → 2026-08-22。</p>
        <div class="hero-rail"><i class="live-dot" aria-hidden="true"></i><span>数据快照 · <strong id="snapshot-date">2026-08-23</strong> 北京时间</span><a href="#method">查看口径</a><span class="scroll-cue"><i></i>向下浏览</span></div>
      </div>
    </section>

    <section class="section section--tight reveal" aria-label="快照指标">
      <div class="metrics">
        <div class="metric"><span class="metric-number" id="metric-days">30</span><span class="metric-label">日报锚点</span></div>
        <div class="metric"><span class="metric-number" id="metric-hotspots">4</span><span class="metric-label">当前热点</span></div>
        <div class="metric"><span class="metric-number" id="metric-recent">30</span><span class="metric-label">近 7 天精选</span></div>
        <div class="metric"><span class="metric-number" id="metric-score">67.6</span><span class="metric-label">精选平均分</span></div>
      </div>
    </section>

    <section class="section reveal" id="radar">
      <div class="section-head">
        <div><span class="section-index">01 / NOW</span><h2>当前热点<br>正在升温。</h2></div>
        <p>AIHOT 热点榜按多源报道与事件信号实时排序。这里展示的是当前榜，不是过去 30 天累计排行。</p>
      </div>
      <div class="hotspot-list" id="hotspot-list"></div>
    </section>

    <section class="section reveal" id="pulse">
      <div class="section-head">
        <div><span class="section-index">02 / PULSE</span><h2>每天一个<br>主叙事。</h2></div>
        <p>30 个日报 lead，组成一条从端侧模型、Agent 基建到具身智能的连续时间线。点击日期可回到 AIHOT 日报。</p>
      </div>
      <div class="pulse-panel"><div class="timeline" id="timeline"></div></div>
    </section>

    <section class="section reveal" id="feed">
      <div class="section-head">
        <div><span class="section-index">03 / EDIT</span><h2>精选流，<br>只留有用的。</h2></div>
        <p>近 7 天精选条目按 AIHOT 评分排序口径保留。筛选类别，展开摘要与 AIHOT 的“为什么值得看”。</p>
      </div>
      <div class="feed-toolbar" role="toolbar" aria-label="精选流筛选">
        <button class="filter" type="button" data-filter="all" aria-pressed="true">全部</button>
        <button class="filter" type="button" data-filter="ai-models" aria-pressed="false">模型</button>
        <button class="filter" type="button" data-filter="ai-products" aria-pressed="false">产品 / 基建</button>
        <button class="filter" type="button" data-filter="industry" aria-pressed="false">行业</button>
        <button class="filter" type="button" data-filter="paper" aria-pressed="false">论文</button>
        <button class="filter" type="button" data-filter="tip" aria-pressed="false">教程 / 观点</button>
      </div>
      <div class="feed-list" id="feed-list"></div>
    </section>

    <section class="section method reveal" id="method">
      <div><span class="section-index">04 / SOURCE</span><h2>先看信号，<br>再点原文。</h2></div>
      <div class="method-copy">
        <p>本页是本地静态快照，不会自动刷新。数据来自 AIHOT 官方匿名只读 API：日报索引覆盖 30 天，当前热点来自热点榜，精选流来自近 7 天精选接口。</p>
        <p>AIHOT 的标题与摘要是第三方原文的聚合与编辑策展；页面保留 AIHOT 站内阅读链接与原文入口，重要数字和原话请回到原文核对。</p>
        <div class="source-note">官方入口：<a href="https://aihot.virxact.com/" target="_blank" rel="noreferrer">AIHOT 首页</a> · <a href="https://aihot.virxact.com/hot" target="_blank" rel="noreferrer">热点榜</a> · <a href="https://aihot.virxact.com/monthly/2026-07" target="_blank" rel="noreferrer">2026-07 月报</a> · <a href="https://aihot.virxact.com/llms.txt" target="_blank" rel="noreferrer">数据说明</a></div>
      </div>
    </section>
  </main>

  <footer class="site-footer"><strong>AIHOT / 30-DAY SIGNALS</strong><span>本地编辑快照 · 2026-08-23 · <a href="#top">返回顶部 ↑</a></span></footer>

  <script type="application/json" id="snapshot-data">__SNAPSHOT_DATA__</script>
  <script>
    const DATA = JSON.parse(document.getElementById('snapshot-data').textContent);
    const categoryLabels = { 'ai-models': 'MODEL', 'ai-products': 'PRODUCT / INFRA', industry: 'INDUSTRY', paper: 'PAPER', tip: 'FIELD NOTE' };
    const escapeHTML = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
    const dateLabel = (value, withYear = false) => {
      const date = new Date(value.includes('T') ? value : `${value}T00:00:00+08:00`);
      return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', ...(withYear ? { year: 'numeric' } : {}) }).format(date).replace(/\//g, '.');
    };
    const formatTitle = (title) => title.replace(/\s+/g, ' ').trim();

    document.getElementById('snapshot-date').textContent = DATA.snapshotDate;
    document.getElementById('metric-days').textContent = DATA.metrics.dailyCount;
    document.getElementById('metric-hotspots').textContent = DATA.metrics.hotspotCount;
    document.getElementById('metric-recent').textContent = DATA.metrics.recentCount;
    document.getElementById('metric-score').textContent = DATA.metrics.averageScore ?? '—';

    document.getElementById('hotspot-list').innerHTML = DATA.hotspots.map((item) => `
      <article class="hotspot-item">
        <div class="hotspot-rank">0${item.rank}</div>
        <div>
          <h3 class="hotspot-title">${escapeHTML(formatTitle(item.title))}</h3>
          <p class="hotspot-source">${escapeHTML(item.source?.name || 'AIHOT 聚合')} · 最近更新 ${escapeHTML(dateLabel(item.latestAt))}</p>
        </div>
        <div class="hotspot-sources"><strong>${item.sourceCount || 0} 个独立信源</strong>${escapeHTML((item.sourceNames || []).slice(0, 3).join(' · '))}${item.sourceNames?.length > 3 ? ' · …' : ''}</div>
        <div class="signal-meta"><strong>${item.signalCount || 0}</strong> 条信号<a href="${escapeHTML(item.links?.story || item.links?.aihot)}" target="_blank" rel="noreferrer">查看事件 ↗</a></div>
      </article>
    `).join('');

    document.getElementById('timeline').innerHTML = DATA.daily.map((item, index) => `
      <article class="timeline-item" style="--i:${index}">
        <time class="timeline-date" datetime="${escapeHTML(item.date)}">${escapeHTML(dateLabel(item.date, true))}</time>
        <i class="timeline-dot" aria-hidden="true"></i>
        <h3 class="timeline-title">${escapeHTML(formatTitle(item.leadTitle))}</h3>
        <a class="timeline-link" href="${escapeHTML(item.links?.aihot || item.attribution?.url)}" target="_blank" rel="noreferrer">打开日报 ↗</a>
      </article>
    `).join('');

    const renderFeed = (filter = 'all') => {
      const items = DATA.recent.filter((item) => filter === 'all' || item.category === filter);
      document.getElementById('feed-list').innerHTML = items.length ? items.map((item) => `
        <article class="news-row">
          <div class="news-meta"><div class="news-kicker">${escapeHTML(categoryLabels[item.category] || 'SIGNAL')}</div><div class="news-date">${escapeHTML(dateLabel(item.publishedAt, true))}</div></div>
          <div class="news-content">
            <h3>${escapeHTML(formatTitle(item.title))}</h3>
            <p>${escapeHTML(item.summary)}</p>
            <p class="news-reason">${escapeHTML(item.reason || 'AIHOT 精选条目')}</p>
            <div class="news-links"><a href="${escapeHTML(item.links?.aihot)}" target="_blank" rel="noreferrer">AIHOT 阅读 ↗</a><a href="${escapeHTML(item.links?.original)}" target="_blank" rel="noreferrer">原文入口 ↗</a></div>
          </div>
          <div class="news-score"><strong>${escapeHTML(item.score)}</strong>AIHOT 分</div>
        </article>
      `).join('') : '<p class="empty">这个筛选下暂无条目。</p>';
    };
    renderFeed();
    document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => {
      document.querySelectorAll('[data-filter]').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
      renderFeed(button.dataset.filter);
    }));

    const revealObserver = new IntersectionObserver((entries) => entries.forEach((entry) => entry.isIntersecting && entry.target.classList.add('is-visible')), { threshold: .12 });
    document.querySelectorAll('.reveal').forEach((element) => revealObserver.observe(element));

    const updateScrollState = () => {
      const max = document.documentElement.scrollHeight - window.innerHeight;
      document.getElementById('progress').style.transform = `scaleX(${max > 0 ? window.scrollY / max : 0})`;
      document.documentElement.style.setProperty('--scroll-y', `${window.scrollY}px`);
    };
    window.addEventListener('scroll', updateScrollState, { passive: true });
    window.addEventListener('resize', updateScrollState);
    updateScrollState();
    window.addEventListener('pointermove', (event) => {
      document.documentElement.style.setProperty('--mx', `${event.clientX}px`);
      document.documentElement.style.setProperty('--my', `${event.clientY}px`);
    }, { passive: true });
  </script>
</body>
</html>
'''


if __name__ == "__main__":
    build()
