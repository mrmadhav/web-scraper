#!/usr/bin/env python3
"""Local UI to run scraper.py with live progress updates."""

from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template_string, request


APP_ROOT = Path(__file__).resolve().parent
RUNS_DIR = APP_ROOT / ".runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

MAX_EVENTS = 500
POLL_SECONDS = 0.4

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

SELECTOR_PREVIEW_CSS = """
<style id="codex-selector-style">
  body { cursor: crosshair !important; }
  .codex-selector-hud {
    position: fixed;
    top: 10px;
    right: 10px;
    z-index: 2147483647;
    background: rgba(30, 46, 44, 0.92);
    color: #fff;
    border-radius: 999px;
    font: 600 12px/1.2 Arial, sans-serif;
    padding: 7px 10px;
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.25);
    pointer-events: none;
  }
</style>
"""

SELECTOR_PREVIEW_SCRIPT = """
<script id="codex-selector-script">
(function () {
  var hud = document.createElement('div');
  hud.className = 'codex-selector-hud';
  hud.textContent = 'Selector Mode: click an element';
  document.documentElement.appendChild(hud);

  var highlighted = null;

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
    return String(value).replace(/([ !"#$%&'()*+,./:;<=>?@[\\\\\\]^`{|}~])/g, '\\\\$1');
  }

  function shortText(value) {
    var text = (value || '').replace(/\\s+/g, ' ').trim();
    if (text.length > 140) return text.slice(0, 140) + '...';
    return text;
  }

  function selectorFor(element) {
    if (!element || element.nodeType !== 1) return '';
    if (element.id) return '#' + cssEscape(element.id);

    var parts = [];
    var node = element;
    while (node && node.nodeType === 1 && node.tagName.toLowerCase() !== 'html') {
      var part = node.tagName.toLowerCase();
      if (node.id) {
        part += '#' + cssEscape(node.id);
        parts.unshift(part);
        break;
      }

      var classList = [];
      if (typeof node.className === 'string') {
        classList = node.className.split(/\\s+/).filter(Boolean).slice(0, 2);
      }
      if (classList.length) {
        part += '.' + classList.map(cssEscape).join('.');
      }

      if (node.parentElement) {
        var siblings = Array.prototype.filter.call(node.parentElement.children, function (sib) {
          return sib.tagName === node.tagName;
        });
        if (siblings.length > 1) {
          part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
        }
      }

      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  }

  function nearestId(element) {
    var node = element;
    while (node && node.nodeType === 1 && node !== document.body) {
      if (node.id) return node.id;
      node = node.parentElement;
    }
    return '';
  }

  function anchorOf(element) {
    if (!element || typeof element.closest !== 'function') return null;
    return element.closest('a');
  }

  function articleSuggestion(anchor) {
    if (!anchor) return '';
    var article = anchor.closest('article');
    if (article) return 'article a[href]';

    var classes = [];
    if (typeof anchor.className === 'string') {
      classes = anchor.className.split(/\\s+/).filter(Boolean).slice(0, 2);
    }
    if (classes.length) {
      return 'a.' + classes.map(cssEscape).join('.') + '[href]';
    }
    return 'a[href]';
  }

  function paginationSuggestion(anchor) {
    if (!anchor) return '';
    var wrap = anchor.closest('[class*="pagination"], [id*="pagination"], [class*="pager"], [id*="pager"]');
    if (wrap) {
      if (wrap.id) return '#' + cssEscape(wrap.id) + ' a[href]';
      var classes = [];
      if (typeof wrap.className === 'string') {
        classes = wrap.className.split(/\\s+/).filter(Boolean).slice(0, 2);
      }
      if (classes.length) {
        return wrap.tagName.toLowerCase() + '.' + classes.map(cssEscape).join('.') + ' a[href]';
      }
      return wrap.tagName.toLowerCase() + ' a[href]';
    }
    return 'a[href*="/page/"], a[rel~="next"]';
  }

  function highlight(element) {
    if (highlighted === element) return;
    if (highlighted) highlighted.style.outline = highlighted.dataset.codexOldOutline || '';
    highlighted = element;
    if (highlighted) {
      highlighted.dataset.codexOldOutline = highlighted.style.outline || '';
      highlighted.style.outline = '2px solid #ca5b27';
    }
  }

  function sendSelection(element) {
    var anchor = anchorOf(element);
    var payload = {
      source: 'selector-preview',
      type: 'element-selected',
      data: {
        tag: (element.tagName || '').toLowerCase(),
        id: element.id || '',
        class_name: typeof element.className === 'string' ? element.className : '',
        text_preview: shortText(element.innerText || element.textContent || ''),
        selector: selectorFor(element),
        closest_id: nearestId(element),
        anchor_selector: anchor ? selectorFor(anchor) : '',
        anchor_href: anchor ? (anchor.getAttribute('href') || '') : '',
        article_selector_suggestion: articleSuggestion(anchor),
        pagination_selector_suggestion: paginationSuggestion(anchor),
      }
    };
    window.parent.postMessage(payload, '*');
  }

  document.addEventListener('mouseover', function (event) {
    highlight(event.target);
  }, true);

  document.addEventListener('click', function (event) {
    event.preventDefault();
    event.stopPropagation();
    sendSelection(event.target);
  }, true);
})();
</script>
"""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Scraper Control Room</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <style>
    :root {
      --bg-top: #f5efe2;
      --bg-bottom: #d6e8dc;
      --panel: #fffaf0;
      --ink: #1e2e2c;
      --muted: #5d6d69;
      --accent: #ca5b27;
      --accent-soft: #e38e64;
      --success: #1f8f6d;
      --warn: #ae4621;
      --card-border: #d5cbb7;
      --shadow: 0 20px 40px rgba(18, 42, 40, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Space Grotesk", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 10% 10%, rgba(202, 91, 39, 0.16) 0, transparent 45%),
        radial-gradient(circle at 85% 18%, rgba(31, 143, 109, 0.18) 0, transparent 38%),
        linear-gradient(165deg, var(--bg-top) 0%, var(--bg-bottom) 100%);
      padding: 28px;
    }

    .shell {
      max-width: 1240px;
      margin: 0 auto;
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(360px, 1fr);
      gap: 20px;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--card-border);
      border-radius: 20px;
      box-shadow: var(--shadow);
      padding: 20px;
    }

    .headline {
      margin: 0 0 8px 0;
      font-size: 1.5rem;
      letter-spacing: 0.02em;
    }

    .subline {
      margin: 0;
      color: var(--muted);
      line-height: 1.45;
      font-size: 0.96rem;
    }

    .form-grid {
      display: grid;
      gap: 12px;
      margin-top: 16px;
    }

    .grid-two {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    label {
      display: block;
      font-size: 0.83rem;
      color: var(--muted);
      margin-bottom: 6px;
      letter-spacing: 0.01em;
    }

    input[type="text"],
    input[type="number"] {
      width: 100%;
      border-radius: 10px;
      border: 1px solid #cfc3ad;
      padding: 10px 12px;
      font-size: 0.95rem;
      font-family: "IBM Plex Mono", monospace;
      background: #fffdf8;
      color: var(--ink);
    }

    input:focus {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(202, 91, 39, 0.18);
    }

    .checkline {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 0.9rem;
      color: var(--muted);
      margin-top: 4px;
    }

    .checkline input {
      transform: translateY(1px);
    }

    .cta {
      margin-top: 8px;
      border: 0;
      border-radius: 12px;
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-soft) 100%);
      color: #fff;
      font-size: 0.98rem;
      font-weight: 600;
      padding: 12px 16px;
      cursor: pointer;
      transition: transform 0.12s ease, box-shadow 0.2s ease;
      box-shadow: 0 8px 18px rgba(202, 91, 39, 0.32);
    }

    .cta:hover {
      transform: translateY(-1px);
    }

    .cta:disabled {
      cursor: not-allowed;
      opacity: 0.7;
      transform: none;
    }

    .status-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-top: 6px;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 6px 12px;
      font-size: 0.78rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      background: #efe6d4;
      color: #665a43;
    }

    .badge.running {
      background: #e7f3ee;
      color: var(--success);
    }

    .badge.completed {
      background: #daf2e9;
      color: var(--success);
    }

    .badge.failed {
      background: #f6dfd4;
      color: var(--warn);
    }

    .job-id {
      font-family: "IBM Plex Mono", monospace;
      font-size: 0.8rem;
      color: var(--muted);
    }

    .meter-wrap {
      margin-top: 14px;
      background: #f0e5d1;
      border-radius: 999px;
      height: 10px;
      overflow: hidden;
    }

    .meter {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, #1f8f6d 0%, #45ba8f 100%);
      transition: width 0.2s ease;
    }

    .stats {
      margin-top: 14px;
      display: grid;
      grid-template-columns: repeat(4, minmax(70px, 1fr));
      gap: 8px;
    }

    .stat {
      border: 1px solid #d7ccb8;
      border-radius: 10px;
      padding: 8px 10px;
      background: #fffdfa;
    }

    .stat .k {
      font-size: 0.7rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .stat .v {
      margin-top: 2px;
      font-size: 1.2rem;
      font-weight: 700;
    }

    .steps {
      margin-top: 18px;
      display: grid;
      gap: 7px;
    }

    .step {
      border: 1px solid #d8ccb7;
      border-radius: 10px;
      padding: 9px 11px;
      background: #fffcf8;
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.9rem;
    }

    .step .dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: #d9ccb6;
      margin-right: 8px;
      flex-shrink: 0;
    }

    .step-label {
      display: flex;
      align-items: center;
      color: #4f615c;
    }

    .step.done .dot {
      background: var(--success);
    }

    .step.active {
      border-color: var(--accent);
      box-shadow: inset 0 0 0 1px rgba(202, 91, 39, 0.15);
      background: #fff7f2;
    }

    .step-state {
      font-family: "IBM Plex Mono", monospace;
      font-size: 0.72rem;
      text-transform: uppercase;
      color: #7a6b54;
      letter-spacing: 0.05em;
    }

    .log-wrap {
      margin-top: 14px;
      border: 1px solid #d6cab6;
      border-radius: 12px;
      background: #fefaf2;
      max-height: 390px;
      overflow: auto;
      padding: 4px 0;
    }

    .event {
      display: grid;
      grid-template-columns: 104px 1fr;
      gap: 8px;
      padding: 8px 12px;
      border-bottom: 1px solid #ece0cb;
      font-size: 0.85rem;
      line-height: 1.35;
    }

    .event:last-child {
      border-bottom: none;
    }

    .event-time {
      font-family: "IBM Plex Mono", monospace;
      color: #85755f;
      font-size: 0.76rem;
      white-space: nowrap;
    }

    .event-msg {
      color: #314541;
    }

    .empty {
      color: #7e725e;
      font-size: 0.9rem;
      padding: 12px;
    }

    .hint {
      margin-top: 10px;
      font-size: 0.84rem;
      color: #6f6553;
      line-height: 1.35;
    }

    .selector-open {
      margin-top: 0;
      background: #fff2e9;
      color: #85462a;
      box-shadow: none;
      border: 1px solid #e8c5b0;
    }

    .selector-open:hover {
      background: #ffe8db;
      transform: none;
    }

    .selector-modal {
      position: fixed;
      inset: 0;
      background: rgba(20, 26, 25, 0.52);
      z-index: 1000;
      display: none;
      padding: 20px;
    }

    .selector-modal.open {
      display: block;
    }

    .selector-dialog {
      width: min(1200px, 100%);
      height: min(88vh, 900px);
      margin: 0 auto;
      border-radius: 16px;
      overflow: hidden;
      border: 1px solid #ccbfa8;
      background: #fef8ee;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.22);
      display: grid;
      grid-template-rows: auto 1fr;
    }

    .selector-top {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
      padding: 12px 14px;
      background: #f6ebd8;
      border-bottom: 1px solid #daccb4;
    }

    .selector-title {
      font-size: 0.95rem;
      font-weight: 700;
      color: #3c4f4b;
    }

    .selector-controls {
      display: flex;
      gap: 8px;
      align-items: center;
      min-width: 0;
    }

    .selector-controls input {
      width: 460px;
      max-width: 48vw;
    }

    .selector-btn {
      border: 1px solid #d6bea5;
      border-radius: 9px;
      background: #fffaf2;
      color: #5d4938;
      padding: 8px 11px;
      font-size: 0.84rem;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
    }

    .selector-btn:hover {
      background: #fef1e3;
    }

    .selector-body {
      display: grid;
      grid-template-columns: 310px 1fr;
      min-height: 0;
    }

    .selector-side {
      border-right: 1px solid #dfd1ba;
      background: #fffdf8;
      padding: 12px;
      overflow: auto;
    }

    .selector-side h3 {
      margin: 0 0 8px 0;
      font-size: 0.95rem;
    }

    .selector-help {
      font-size: 0.83rem;
      color: #6c624f;
      line-height: 1.35;
      margin-bottom: 12px;
    }

    .selector-picked {
      border: 1px solid #e2d6c1;
      border-radius: 10px;
      padding: 8px;
      background: #fff;
      font-family: "IBM Plex Mono", monospace;
      font-size: 0.78rem;
      color: #495a56;
      line-height: 1.35;
      word-break: break-word;
    }

    .selector-actions {
      margin-top: 10px;
      display: grid;
      gap: 8px;
    }

    .selector-actions .selector-btn:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }

    .selector-note {
      margin-top: 10px;
      font-size: 0.8rem;
      color: #665a48;
      line-height: 1.35;
      min-height: 2.4em;
    }

    .selector-canvas {
      min-width: 0;
      min-height: 0;
      background: #fff;
    }

    .selector-frame {
      width: 100%;
      height: 100%;
      border: 0;
      background: #fff;
    }

    @media (max-width: 1024px) {
      .shell {
        grid-template-columns: 1fr;
      }

      .selector-top {
        grid-template-columns: 1fr;
      }

      .selector-controls input {
        width: 100%;
        max-width: none;
      }

      .selector-body {
        grid-template-columns: 1fr;
      }

      .selector-side {
        border-right: 0;
        border-bottom: 1px solid #dfd1ba;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="panel">
      <h1 class="headline">Scraper Control Room</h1>
      <p class="subline">Configure your crawl, then watch each step: start, crawl, link discovery, extraction, and finish.</p>

      <form id="crawl-form" class="form-grid">
        <div>
          <label for="url">Start URL</label>
          <input id="url" name="url" type="text" placeholder="https://example.com/blog" required />
        </div>
        <button id="open-selector-btn" type="button" class="cta selector-open">Open Visual Selector</button>

        <div>
          <label for="output_dir">Output Folder</label>
          <input id="output_dir" name="output_dir" type="text" value="output" />
        </div>

        <div class="grid-two">
          <div>
            <label for="max_pages">Max Pages</label>
            <input id="max_pages" name="max_pages" type="number" min="1" value="80" />
          </div>
          <div>
            <label for="max_depth">Max Depth</label>
            <input id="max_depth" name="max_depth" type="number" min="0" value="2" />
          </div>
        </div>

        <div class="grid-two">
          <div>
            <label for="min_words">Min Words</label>
            <input id="min_words" name="min_words" type="number" min="1" value="180" />
          </div>
          <div>
            <label for="concurrency">Concurrency</label>
            <input id="concurrency" name="concurrency" type="number" min="1" value="2" />
          </div>
        </div>

        <div>
          <label for="download_delay">Download Delay (seconds)</label>
          <input id="download_delay" name="download_delay" type="number" min="0" step="0.05" value="0.5" />
        </div>

        <div>
          <label for="container_id">Container Id (optional)</label>
          <input id="container_id" name="container_id" type="text" placeholder="main-content" />
        </div>

        <div>
          <label for="article_selector">Article Selector (optional)</label>
          <input id="article_selector" name="article_selector" type="text" placeholder="h2 a" />
        </div>

        <div>
          <label for="pagination_selector">Pagination Selector (optional)</label>
          <input id="pagination_selector" name="pagination_selector" type="text" placeholder=".pagination a" />
        </div>

        <div class="grid-two">
          <div>
            <label for="max_pagination_pages">Max Pagination Pages (optional)</label>
            <input id="max_pagination_pages" name="max_pagination_pages" type="number" min="1" value="5" />
          </div>
          <div>
            <label for="article_url_regex">Article URL Regex (optional)</label>
            <input id="article_url_regex" name="article_url_regex" type="text" placeholder="/Thematique/techno-ux-1226/" />
          </div>
        </div>

        <label class="checkline">
          <input id="pagination_follow_next_only" name="pagination_follow_next_only" type="checkbox" checked />
          Follow only next pagination page
        </label>

        <label class="checkline">
          <input id="obey_robots" name="obey_robots" type="checkbox" />
          Respect robots.txt
        </label>

        <button id="start-btn" type="submit" class="cta">Start Crawl</button>
      </form>

      <p class="hint">Tip: If a site blocks requests, lower concurrency and increase delay. Container mode is ideal for article listings + pagination pages.</p>
    </section>

    <section class="panel">
      <div class="status-row">
        <div>
          <div id="status-badge" class="badge">idle</div>
          <div id="job-id" class="job-id">No job running</div>
        </div>
        <div id="output-path" class="job-id"></div>
      </div>

      <div class="meter-wrap">
        <div id="meter" class="meter"></div>
      </div>

      <div class="stats">
        <div class="stat"><div class="k">Visited</div><div id="stat-visited" class="v">0</div></div>
        <div class="stat"><div class="k">Saved</div><div id="stat-saved" class="v">0</div></div>
        <div class="stat"><div class="k">Blocked</div><div id="stat-blocked" class="v">0</div></div>
        <div class="stat"><div class="k">Queued</div><div id="stat-queued" class="v">0</div></div>
      </div>

      <div class="steps" id="steps">
        <div class="step" data-step="init"><div class="step-label"><span class="dot"></span>Initialize Crawl</div><div class="step-state">pending</div></div>
        <div class="step" data-step="visit"><div class="step-label"><span class="dot"></span>Visit Pages</div><div class="step-state">pending</div></div>
        <div class="step" data-step="discover"><div class="step-label"><span class="dot"></span>Discover Links</div><div class="step-state">pending</div></div>
        <div class="step" data-step="save"><div class="step-label"><span class="dot"></span>Extract + Save</div><div class="step-state">pending</div></div>
        <div class="step" data-step="finish"><div class="step-label"><span class="dot"></span>Complete</div><div class="step-state">pending</div></div>
      </div>

      <div id="events" class="log-wrap">
        <div class="empty">Run a crawl to see step-by-step progress.</div>
      </div>
    </section>
  </div>

  <div id="selector-modal" class="selector-modal" aria-hidden="true">
    <div class="selector-dialog">
      <div class="selector-top">
        <div class="selector-title">Visual Selector</div>
        <div class="selector-controls">
          <input id="selector-url" type="text" placeholder="https://example.com/page" />
          <button id="selector-load-btn" type="button" class="selector-btn">Load Page</button>
          <button id="selector-close-btn" type="button" class="selector-btn">Close</button>
        </div>
      </div>
      <div class="selector-body">
        <aside class="selector-side">
          <h3>How It Works</h3>
          <p class="selector-help">Load the page, click an element in the preview, then assign it as container, article selector, or pagination selector.</p>
          <div id="selector-picked" class="selector-picked">No element selected yet.</div>
          <div class="selector-actions">
            <button id="apply-container-btn" type="button" class="selector-btn" disabled>Use as Container Id</button>
            <button id="apply-article-btn" type="button" class="selector-btn" disabled>Use as Article Selector</button>
            <button id="apply-pagination-btn" type="button" class="selector-btn" disabled>Use as Pagination Selector</button>
          </div>
          <div id="selector-note" class="selector-note"></div>
        </aside>
        <div class="selector-canvas">
          <iframe id="selector-frame" class="selector-frame" title="Visual selector preview"></iframe>
        </div>
      </div>
    </div>
  </div>

  <script>
    const form = document.getElementById("crawl-form");
    const startBtn = document.getElementById("start-btn");
    const openSelectorBtn = document.getElementById("open-selector-btn");
    const statusBadge = document.getElementById("status-badge");
    const jobIdEl = document.getElementById("job-id");
    const meter = document.getElementById("meter");
    const outputPathEl = document.getElementById("output-path");
    const eventsEl = document.getElementById("events");
    const selectorModal = document.getElementById("selector-modal");
    const selectorUrlInput = document.getElementById("selector-url");
    const selectorLoadBtn = document.getElementById("selector-load-btn");
    const selectorCloseBtn = document.getElementById("selector-close-btn");
    const selectorFrame = document.getElementById("selector-frame");
    const selectorPicked = document.getElementById("selector-picked");
    const selectorNote = document.getElementById("selector-note");
    const applyContainerBtn = document.getElementById("apply-container-btn");
    const applyArticleBtn = document.getElementById("apply-article-btn");
    const applyPaginationBtn = document.getElementById("apply-pagination-btn");
    const containerInput = document.getElementById("container_id");
    const articleInput = document.getElementById("article_selector");
    const paginationInput = document.getElementById("pagination_selector");
    const stepEls = {
      init: document.querySelector('[data-step="init"]'),
      visit: document.querySelector('[data-step="visit"]'),
      discover: document.querySelector('[data-step="discover"]'),
      save: document.querySelector('[data-step="save"]'),
      finish: document.querySelector('[data-step="finish"]'),
    };

    const stats = {
      visited: document.getElementById("stat-visited"),
      saved: document.getElementById("stat-saved"),
      blocked: document.getElementById("stat-blocked"),
      queued: document.getElementById("stat-queued"),
    };

    let currentJobId = null;
    let poller = null;
    let lastSelectedElement = null;

    function openSelector() {
      selectorModal.classList.add("open");
      selectorModal.setAttribute("aria-hidden", "false");
      if (!selectorUrlInput.value.trim()) {
        selectorUrlInput.value = document.getElementById("url").value.trim();
      }
    }

    function closeSelector() {
      selectorModal.classList.remove("open");
      selectorModal.setAttribute("aria-hidden", "true");
    }

    function setSelectorNote(message) {
      selectorNote.textContent = message || "";
    }

    function renderSelectedElement(data) {
      const hasSelection = !!data;
      applyContainerBtn.disabled = !hasSelection;
      applyArticleBtn.disabled = !hasSelection;
      applyPaginationBtn.disabled = !hasSelection;

      if (!hasSelection) {
        selectorPicked.textContent = "No element selected yet.";
        return;
      }

      const rows = [
        `tag: ${data.tag || "-"}`,
        `id: ${data.id || "-"}`,
        `closest_id: ${data.closest_id || "-"}`,
        `selector: ${data.selector || "-"}`,
        `anchor_selector: ${data.anchor_selector || "-"}`,
        `text: ${data.text_preview || "-"}`,
      ];
      selectorPicked.textContent = rows.join("\\n");
    }

    function isHttpUrl(value) {
      return /^https?:\\/\\//i.test(value || "");
    }

    function loadSelectorPreview() {
      const target = selectorUrlInput.value.trim() || document.getElementById("url").value.trim();
      if (!isHttpUrl(target)) {
        setSelectorNote("Enter a valid http(s) URL.");
        return;
      }
      selectorUrlInput.value = target;
      selectorFrame.src = `/api/selector/preview?url=${encodeURIComponent(target)}`;
      setSelectorNote("Loading page for selection...");
      lastSelectedElement = null;
      renderSelectedElement(null);
    }

    function applyContainerSelection() {
      if (!lastSelectedElement) return;
      const chosenId = lastSelectedElement.closest_id || lastSelectedElement.id || "";
      if (!chosenId) {
        setSelectorNote("Selected element has no id or parent id. Click a wrapper with an id.");
        return;
      }
      containerInput.value = chosenId;
      setSelectorNote(`Container id set to #${chosenId}`);
    }

    function applyArticleSelection() {
      if (!lastSelectedElement) return;
      const selector = lastSelectedElement.article_selector_suggestion
        || lastSelectedElement.anchor_selector
        || lastSelectedElement.selector;
      if (!selector) {
        setSelectorNote("Could not derive an article selector from this element.");
        return;
      }
      articleInput.value = selector;
      setSelectorNote(`Article selector set to ${selector}`);
    }

    function applyPaginationSelection() {
      if (!lastSelectedElement) return;
      const selector = lastSelectedElement.pagination_selector_suggestion
        || lastSelectedElement.anchor_selector
        || lastSelectedElement.selector;
      if (!selector) {
        setSelectorNote("Could not derive a pagination selector from this element.");
        return;
      }
      paginationInput.value = selector;
      setSelectorNote(`Pagination selector set to ${selector}`);
    }

    function setBadge(status) {
      statusBadge.className = "badge";
      statusBadge.textContent = status;
      if (status === "running" || status === "starting") statusBadge.classList.add("running");
      if (status === "completed") statusBadge.classList.add("completed");
      if (status === "failed") statusBadge.classList.add("failed");
    }

    function setStepState(key, state) {
      const node = stepEls[key];
      if (!node) return;
      node.classList.remove("done", "active");
      const stateEl = node.querySelector(".step-state");
      stateEl.textContent = state;
      if (state === "done") node.classList.add("done");
      if (state === "active") node.classList.add("active");
    }

    function renderSteps(job) {
      const events = job.events || [];
      const eventNames = new Set(events.map((e) => e.event));

      setStepState("init", eventNames.has("started") ? "done" : "pending");
      setStepState("visit", Number(job.metrics.visited || 0) > 0 ? "done" : (job.status === "running" ? "active" : "pending"));
      const discovered = eventNames.has("links_discovered") || eventNames.has("scoped_links");
      setStepState("discover", discovered ? "done" : "pending");
      setStepState("save", Number(job.metrics.saved || 0) > 0 ? "done" : "pending");

      if (job.status === "completed") {
        setStepState("finish", "done");
      } else if (job.status === "failed") {
        setStepState("finish", "failed");
      } else if (job.status === "running") {
        setStepState("finish", "active");
      } else {
        setStepState("finish", "pending");
      }
    }

    function renderEvents(events) {
      if (!events.length) {
        eventsEl.innerHTML = '<div class="empty">Waiting for crawler events...</div>';
        return;
      }
      const recent = events.slice(-120).reverse();
      eventsEl.innerHTML = recent.map((evt) => {
        const rawTs = evt.ts || "";
        const ts = rawTs ? new Date(rawTs).toLocaleTimeString() : "--:--:--";
        const msg = evt.message || evt.event || "event";
        return `<div class="event"><div class="event-time">${ts}</div><div class="event-msg">${msg}</div></div>`;
      }).join("");
    }

    function updateUi(job) {
      setBadge(job.status);
      jobIdEl.textContent = job.id ? `Job: ${job.id}` : "No job running";
      outputPathEl.textContent = job.output_dir ? `Output: ${job.output_dir}` : "";

      const visited = Number(job.metrics.visited || 0);
      const saved = Number(job.metrics.saved || 0);
      const blocked = Number(job.metrics.blocked || 0);
      const queued = Number(job.metrics.queued_article_links || 0) + Number(job.metrics.queued_pagination_links || 0);
      const maxPages = Number(job.params.max_pages || 1);
      const pct = Math.min(100, Math.round((visited / Math.max(1, maxPages)) * 100));

      meter.style.width = `${pct}%`;
      stats.visited.textContent = String(visited);
      stats.saved.textContent = String(saved);
      stats.blocked.textContent = String(blocked);
      stats.queued.textContent = String(queued);

      renderSteps(job);
      renderEvents(job.events || []);
    }

    async function pollJob() {
      if (!currentJobId) return;
      try {
        const res = await fetch(`/api/jobs/${currentJobId}`);
        if (!res.ok) return;
        const payload = await res.json();
        if (!payload.ok) return;
        const job = payload.job;
        updateUi(job);

        if (job.status === "completed" || job.status === "failed") {
          clearInterval(poller);
          poller = null;
          startBtn.disabled = false;
          startBtn.textContent = "Start Crawl";
        }
      } catch (_) {
      }
    }

    renderSelectedElement(null);

    openSelectorBtn.addEventListener("click", () => {
      openSelector();
      loadSelectorPreview();
    });

    selectorLoadBtn.addEventListener("click", loadSelectorPreview);
    selectorCloseBtn.addEventListener("click", closeSelector);
    selectorFrame.addEventListener("load", () => {
      setSelectorNote("Page loaded. Click an element inside the preview.");
    });
    selectorModal.addEventListener("click", (event) => {
      if (event.target === selectorModal) closeSelector();
    });
    applyContainerBtn.addEventListener("click", applyContainerSelection);
    applyArticleBtn.addEventListener("click", applyArticleSelection);
    applyPaginationBtn.addEventListener("click", applyPaginationSelection);

    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && selectorModal.classList.contains("open")) {
        closeSelector();
      }
    });

    window.addEventListener("message", (event) => {
      const payload = event.data || {};
      if (payload.source !== "selector-preview" || payload.type !== "element-selected") return;
      lastSelectedElement = payload.data || null;
      renderSelectedElement(lastSelectedElement);
      setSelectorNote("Element selected. Choose how to apply it.");
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();

      if (poller) {
        clearInterval(poller);
        poller = null;
      }

      const payload = {
        url: document.getElementById("url").value.trim(),
        output_dir: document.getElementById("output_dir").value.trim() || "output",
        max_pages: Number(document.getElementById("max_pages").value || 80),
        max_depth: Number(document.getElementById("max_depth").value || 2),
        min_words: Number(document.getElementById("min_words").value || 180),
        concurrency: Number(document.getElementById("concurrency").value || 2),
        download_delay: Number(document.getElementById("download_delay").value || 0.5),
        container_id: document.getElementById("container_id").value.trim(),
        article_selector: document.getElementById("article_selector").value.trim(),
        pagination_selector: document.getElementById("pagination_selector").value.trim(),
        max_pagination_pages: Number(document.getElementById("max_pagination_pages").value || 0),
        article_url_regex: document.getElementById("article_url_regex").value.trim(),
        pagination_follow_next_only: document.getElementById("pagination_follow_next_only").checked,
        obey_robots: document.getElementById("obey_robots").checked,
      };

      startBtn.disabled = true;
      startBtn.textContent = "Starting...";
      setBadge("starting");

      try {
        const res = await fetch("/api/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
          alert(data.error || "Failed to start crawl");
          startBtn.disabled = false;
          startBtn.textContent = "Start Crawl";
          setBadge("idle");
          return;
        }

        currentJobId = data.job_id;
        startBtn.textContent = "Running...";
        poller = setInterval(pollJob, 1000);
        pollJob();
      } catch (err) {
        alert("Failed to start crawl");
        startBtn.disabled = false;
        startBtn.textContent = "Start Crawl";
        setBadge("idle");
      }
    });
  </script>
</body>
</html>
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_event_time(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


def append_event(job: dict, event: dict) -> None:
    event_copy = {
        "ts": parse_event_time(str(event.get("ts", ""))) or utc_now(),
        "event": str(event.get("event", "unknown")),
        "message": str(event.get("message", "")),
    }
    job["events"].append(event_copy)
    if len(job["events"]) > MAX_EVENTS:
        job["events"] = job["events"][-MAX_EVENTS:]


def update_metrics(job: dict, event: dict) -> None:
    for key in (
        "visited",
        "saved",
        "blocked",
        "queued_article_links",
        "queued_pagination_links",
        "max_pages",
    ):
        if key in event:
            try:
                job["metrics"][key] = int(event[key])
            except (TypeError, ValueError):
                pass
    if "url" in event:
        job["metrics"]["last_url"] = str(event["url"])


def absolute_output_dir(output_dir: str) -> Path:
    value = Path(output_dir).expanduser()
    if value.is_absolute():
        return value
    return APP_ROOT / value


def validate_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def parse_progress_line(line: str) -> dict | None:
    cleaned = line.strip()
    if not cleaned:
        return None
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def inject_before_tag(document: str, tag: str, content: str) -> str:
    pattern = re.compile(rf"</{tag}>", re.IGNORECASE)
    if pattern.search(document):
        return pattern.sub(lambda m: content + m.group(0), document, count=1)
    if tag.lower() == "head":
        html_open = re.compile(r"<html[^>]*>", re.IGNORECASE)
        if html_open.search(document):
            return html_open.sub(lambda m: m.group(0) + "<head>" + content + "</head>", document, count=1)
        return "<head>" + content + "</head>" + document
    return document + content


def remove_remote_scripts(document: str) -> str:
    return re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", document, flags=re.IGNORECASE)


def remove_meta_csp(document: str) -> str:
    return re.sub(
        r"<meta[^>]+http-equiv=[\"']Content-Security-Policy[\"'][^>]*>",
        "",
        document,
        flags=re.IGNORECASE,
    )


def selector_error_page(message: str) -> str:
    safe_message = html.escape(message)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Selector Error</title>"
        "<style>body{font-family:Arial,sans-serif;background:#f9efe1;color:#2f3f3c;padding:24px;}"
        ".box{max-width:760px;border:1px solid #d9c6ab;border-radius:12px;background:#fff7eb;padding:16px;}"
        "h1{margin:0 0 8px 0;font-size:20px;}p{margin:0;line-height:1.4;}</style></head>"
        f"<body><div class='box'><h1>Could not load selector preview</h1><p>{safe_message}</p></div></body></html>"
    )


def build_selector_preview_html(target_url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    response = requests.get(target_url, headers=headers, timeout=30)
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        raise ValueError(f"Target URL did not return HTML. Content-Type was: {content_type or 'unknown'}")

    document = response.text
    document = remove_remote_scripts(document)
    document = remove_meta_csp(document)

    base_href = html.escape(response.url, quote=True)
    head_injections = f'<base href="{base_href}" />' + SELECTOR_PREVIEW_CSS
    document = inject_before_tag(document, "head", head_injections)
    document = inject_before_tag(document, "body", SELECTOR_PREVIEW_SCRIPT)
    return document


def monitor_job(job_id: str, process: subprocess.Popen, progress_path: Path, log_file_handle) -> None:
    offset = 0
    stable_loops_after_exit = 0

    try:
        while True:
            new_events = []
            if progress_path.exists():
                with progress_path.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
                if chunk:
                    for line in chunk.splitlines():
                        event = parse_progress_line(line)
                        if event:
                            new_events.append(event)

            if new_events:
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    if job:
                        for event in new_events:
                            append_event(job, event)
                            update_metrics(job, event)

            rc = process.poll()
            if rc is None:
                time.sleep(POLL_SECONDS)
                continue

            if new_events:
                stable_loops_after_exit = 0
            else:
                stable_loops_after_exit += 1

            if stable_loops_after_exit >= 3:
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    if job:
                        job["return_code"] = rc
                        job["finished_at"] = utc_now()
                        has_finished_event = any(evt.get("event") == "finished" for evt in job["events"])
                        if not has_finished_event:
                            append_event(
                                job,
                                {
                                    "event": "finished",
                                    "message": "Crawler finished",
                                    "ts": utc_now(),
                                    "visited": job["metrics"].get("visited", 0),
                                    "saved": job["metrics"].get("saved", 0),
                                    "blocked": job["metrics"].get("blocked", 0),
                                },
                            )
                        job["status"] = "completed" if rc == 0 else "failed"
                break

            time.sleep(POLL_SECONDS)
    finally:
        log_file_handle.close()


def create_command(payload: dict, progress_file: Path, output_dir: Path) -> list[str]:
    command = [
        sys.executable,
        str(APP_ROOT / "scraper.py"),
        payload["url"],
        "-o",
        str(output_dir),
        "--max-pages",
        str(payload["max_pages"]),
        "--max-depth",
        str(payload["max_depth"]),
        "--min-words",
        str(payload["min_words"]),
        "--concurrency",
        str(payload["concurrency"]),
        "--download-delay",
        str(payload["download_delay"]),
        "--log-level",
        "WARNING",
        "--progress-jsonl",
        str(progress_file),
    ]

    if payload.get("obey_robots"):
        command.append("--obey-robots")
    if payload.get("container_id"):
        command.extend(["--container-id", payload["container_id"]])
    if payload.get("article_selector"):
        command.extend(["--article-selector", payload["article_selector"]])
    if payload.get("pagination_selector"):
        command.extend(["--pagination-selector", payload["pagination_selector"]])
    if payload.get("max_pagination_pages"):
        command.extend(["--max-pagination-pages", str(payload["max_pagination_pages"])])
    if payload.get("pagination_follow_next_only"):
        command.append("--pagination-follow-next-only")
    if payload.get("article_url_regex"):
        command.extend(["--article-url-regex", payload["article_url_regex"]])

    return command


def normalize_payload(raw: dict) -> dict:
    payload = {
        "url": str(raw.get("url", "")).strip(),
        "output_dir": str(raw.get("output_dir", "output")).strip() or "output",
        "max_pages": int(raw.get("max_pages", 80)),
        "max_depth": int(raw.get("max_depth", 2)),
        "min_words": int(raw.get("min_words", 180)),
        "concurrency": int(raw.get("concurrency", 2)),
        "download_delay": float(raw.get("download_delay", 0.5)),
        "container_id": str(raw.get("container_id", "")).strip(),
        "article_selector": str(raw.get("article_selector", "")).strip(),
        "pagination_selector": str(raw.get("pagination_selector", "")).strip(),
        "max_pagination_pages": int(raw.get("max_pagination_pages", 0)),
        "article_url_regex": str(raw.get("article_url_regex", "")).strip(),
        "pagination_follow_next_only": bool(raw.get("pagination_follow_next_only", False)),
        "obey_robots": bool(raw.get("obey_robots", False)),
    }

    payload["max_pages"] = max(1, payload["max_pages"])
    payload["max_depth"] = max(0, payload["max_depth"])
    payload["min_words"] = max(1, payload["min_words"])
    payload["concurrency"] = max(1, payload["concurrency"])
    payload["download_delay"] = max(0.0, payload["download_delay"])
    payload["max_pagination_pages"] = max(0, payload["max_pagination_pages"])
    return payload


app = Flask(__name__)


@app.get("/")
def home():
    return render_template_string(HTML_TEMPLATE)


@app.get("/api/selector/preview")
def selector_preview():
    target_url = str(request.args.get("url", "")).strip()
    if not validate_url(target_url):
        error_html = selector_error_page("Invalid URL. Use a full http(s) URL.")
        return Response(error_html, mimetype="text/html", status=400)

    try:
        preview_html = build_selector_preview_html(target_url)
    except requests.RequestException as exc:
        error_html = selector_error_page(f"Network error while loading URL: {exc}")
        return Response(error_html, mimetype="text/html", status=502)
    except ValueError as exc:
        error_html = selector_error_page(str(exc))
        return Response(error_html, mimetype="text/html", status=400)
    except Exception as exc:
        error_html = selector_error_page(f"Unexpected error: {exc}")
        return Response(error_html, mimetype="text/html", status=500)

    return Response(preview_html, mimetype="text/html")


@app.post("/api/start")
def start_job():
    raw = request.get_json(silent=True) or {}
    try:
        payload = normalize_payload(raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid numeric fields in payload."}), 400

    if not validate_url(payload["url"]):
        return jsonify({"ok": False, "error": "Invalid URL. Use http:// or https://."}), 400

    if (payload["article_selector"] or payload["pagination_selector"]) and not payload["container_id"]:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Provide --container-id when using article/pagination selectors.",
                }
            ),
            400,
        )

    job_id = uuid.uuid4().hex[:10]
    progress_file = RUNS_DIR / f"{job_id}.progress.jsonl"
    log_file = RUNS_DIR / f"{job_id}.log"
    output_dir = absolute_output_dir(payload["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    command = create_command(payload, progress_file, output_dir)

    try:
        log_handle = log_file.open("w", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=str(APP_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Failed to start crawler: {exc}"}), 500

    job = {
        "id": job_id,
        "status": "running",
        "created_at": utc_now(),
        "started_at": utc_now(),
        "finished_at": None,
        "return_code": None,
        "pid": process.pid,
        "command": command,
        "output_dir": str(output_dir),
        "progress_file": str(progress_file),
        "log_file": str(log_file),
        "params": payload,
        "metrics": {
            "visited": 0,
            "saved": 0,
            "blocked": 0,
            "queued_article_links": 0,
            "queued_pagination_links": 0,
            "max_pages": payload["max_pages"],
            "last_url": "",
        },
        "events": [],
    }

    with JOBS_LOCK:
        JOBS[job_id] = job

    append_event(
        job,
        {
            "event": "job_started",
            "message": "Crawler process started",
            "ts": utc_now(),
        },
    )

    monitor = threading.Thread(
        target=monitor_job,
        args=(job_id, process, progress_file, log_handle),
        daemon=True,
    )
    monitor.start()

    return jsonify({"ok": True, "job_id": job_id})


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        snapshot = dict(job)
        snapshot["metrics"] = dict(job["metrics"])
        snapshot["params"] = dict(job["params"])
        snapshot["events"] = list(job["events"])
    return jsonify({"ok": True, "job": snapshot})


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": utc_now()})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False)
