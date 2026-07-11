# -*- coding: utf-8 -*-
import os
import sys
import json
import re
import signal
import subprocess
import shutil
import tempfile
import webbrowser
import uuid
import time
from pathlib import Path
from threading import BoundedSemaphore, Lock, Thread, Timer

import yaml
from flask import Flask, jsonify, request, render_template_string

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = next(
    (candidate for candidate in (SCRIPT_DIR, SCRIPT_DIR.parent) if (candidate / "probhub").is_dir()),
    SCRIPT_DIR.parent,
)
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
LOCAL_JUDGE_SCRIPT = next(
    (candidate for candidate in (SCRIPT_DIR / "local_judge.py", SCRIPT_DIR / "scripts" / "local_judge.py", PACKAGE_ROOT / "scripts" / "local_judge.py") if candidate.is_file()),
    SCRIPT_DIR / "local_judge.py",
)

from probhub.build_lock import workspace_build_lock
from probhub.building import build_workspace, create_build_plan, create_build_snapshot
from probhub.errors import ProbHubError
from probhub.process_control import (
    run_managed_to_files,
    snapshot_process_tree,
    terminate_external_process_tree,
)
from probhub.submissions import (
    MAX_SOURCE_BYTES,
    cleanup_stale_submission_workspaces,
    temporary_submission_workspace,
    validate_cpp_upload,
)
from probhub.typesetting import compile_collection
from probhub.webui_workspace import (
    load_contest_config,
    load_editor_data,
    save_contest_config as save_schema_contest_config,
    save_editor_data,
    schema_workspace_for_subtitle,
)
from probhub.workspace import load_workspace, problem_entries

app = Flask(__name__)
MAX_SUBMISSION_REQUEST_BYTES = MAX_SOURCE_BYTES + 64 * 1024

BASE_DIR = "typst-statement"
SANDBOX_JOBS = {}
SANDBOX_LOCK = Lock()
SUBMISSION_JOBS = {}
SUBMISSION_PROCESSES = {}
SUBMISSION_CANCEL_FILES = {}
SUBMISSION_LOCK = Lock()
SUBMISSION_RESULT_TTL = 60 * 60
SUBMISSION_FORCE_CANCEL_AFTER = 3.0
MAX_CONCURRENT_SUBMISSIONS = max(1, min(4, os.cpu_count() or 2))
SUBMISSION_SLOTS = BoundedSemaphore(MAX_CONCURRENT_SUBMISSIONS)
WEBUI_PREVIEW_TEMP = tempfile.TemporaryDirectory(prefix="probhub-webui-preview-")
WEBUI_PREVIEW_ROOT = Path(WEBUI_PREVIEW_TEMP.name)

try:
    cleanup_stale_submission_workspaces(Path.cwd())
except OSError:
    pass


def secure_path(subtitle, filename):
    """安全路径拼接，防止路径穿越攻击"""
    if not subtitle or '..' in subtitle or '/' in subtitle or '\\' in subtitle:
        raise ValueError("Invalid subtitle")
    return os.path.join(BASE_DIR, subtitle, filename)


def _schema_workspace(subtitle):
    return schema_workspace_for_subtitle(Path.cwd(), subtitle)


def _preview_pdf_path(subtitle):
    if not subtitle or '..' in subtitle or '/' in subtitle or '\\' in subtitle:
        raise ValueError("Invalid subtitle")
    return WEBUI_PREVIEW_ROOT / subtitle / "main.pdf"


def problem_limits(problem_entry):
    problem = (problem_entry or {}).get("problem", {})
    try:
        time_limit = float(problem.get("time_limit", 1))
    except (TypeError, ValueError):
        time_limit = 1.0
    try:
        memory_limit = int(float(problem.get("memory_limit", 256)))
    except (TypeError, ValueError):
        memory_limit = 256
    time_limit = max(time_limit, 0.1)
    memory_limit = max(memory_limit, 1)
    if float(time_limit).is_integer():
        time_limit = int(time_limit)
    return time_limit, memory_limit


def read_probhub_config_from_dir(prob_dir):
    config_path = os.path.join(prob_dir, "probhub.yaml")
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as stream:
            config = yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError):
        return None
    return config if isinstance(config, dict) else None


def _config_entry_file(entry):
    return entry.get("file") if isinstance(entry, dict) else entry


def _config_entries(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _normalized_config_path(entry):
    path = _config_entry_file(entry)
    return str(path).replace("\\", "/") if path else None


def read_problem_limits_from_dir(prob_dir):
    time_limit = 1.0
    memory_limit = 256
    has_meta_time_limit = False
    has_meta_memory_limit = False

    config = read_probhub_config_from_dir(prob_dir)
    if config is not None:
        limits = config.get("limits") or {}
        try:
            if "time" in limits:
                time_limit = float(limits.get("time"))
                has_meta_time_limit = True
            if "memory" in limits:
                memory_limit = int(float(limits.get("memory")))
                has_meta_memory_limit = True
        except (TypeError, ValueError):
            pass

    meta_path = os.path.join(prob_dir, "meta.json")
    if (not has_meta_time_limit or not has_meta_memory_limit) and os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            problem = meta.get("problem", {})
            if not has_meta_time_limit and "time_limit" in problem:
                time_limit = float(problem.get("time_limit"))
                has_meta_time_limit = True
            if not has_meta_memory_limit and "memory_limit" in problem:
                memory_limit = int(float(problem.get("memory_limit")))
                has_meta_memory_limit = True
        except (TypeError, ValueError, OSError, json.JSONDecodeError):
            pass

    ini_path = os.path.join(prob_dir, "domjudge-problem.ini")
    if not has_meta_time_limit and os.path.exists(ini_path):
        try:
            with open(ini_path, "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"timelimit\s*=\s*['\"]?([0-9.]+)", text)
            if m:
                time_limit = float(m.group(1))
        except (TypeError, ValueError, OSError):
            pass

    yaml_path = os.path.join(prob_dir, "problem.yaml")
    if not has_meta_memory_limit and os.path.exists(yaml_path):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"(?m)^\s*memory\s*:\s*([0-9]+)", text)
            if m:
                memory_limit = int(m.group(1))
        except (TypeError, ValueError, OSError):
            pass

    time_limit = max(time_limit, 0.1)
    memory_limit = max(memory_limit, 1)
    if float(time_limit).is_integer():
        time_limit = int(time_limit)
    return time_limit, memory_limit


def enrich_problem_limits(problem_entries):
    name_to_dir = find_problem_dirs()
    for entry in problem_entries:
        problem = entry.setdefault("problem", {})
        if "time_limit" in problem and "memory_limit" in problem:
            continue
        display_name = problem.get("display_name", "")
        prob_dir = name_to_dir.get(display_name)
        if prob_dir:
            time_limit, memory_limit = read_problem_limits_from_dir(prob_dir)
        else:
            time_limit, memory_limit = problem_limits(entry)
        if "time_limit" not in problem:
            problem["time_limit"] = time_limit
        if "memory_limit" not in problem:
            problem["memory_limit"] = memory_limit
    return problem_entries

# ==========================================
# 前端 UI 模板 (TailwindCSS + Alpine.js + SortableJS + Marked + MathJax)
# ==========================================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>ProbHub · 题目排版控制台</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&family=Noto+Serif+SC:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script>
        (() => {
            const saved = localStorage.getItem('probhub-theme');
            const preferred = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
            document.documentElement.dataset.theme = saved === 'light' || saved === 'dark' ? saved : preferred;
        })();
    </script>
    <script src="https://cdn.tailwindcss.com?plugins=typography"></script>
    <script>
      tailwind = { 
        config: {
          theme: {
            extend: {
              colors: {
                ink: {
                  bg:'rgb(var(--ink-bg) / <alpha-value>)', card:'rgb(var(--ink-card) / <alpha-value>)',
                  elevated:'rgb(var(--ink-elevated) / <alpha-value>)', input:'rgb(var(--ink-input) / <alpha-value>)',
                  border:'rgb(var(--ink-border) / <alpha-value>)', deep:'rgb(var(--ink-deep) / <alpha-value>)'
                },
                gold: {
                  DEFAULT:'rgb(var(--gold) / <alpha-value>)', light:'rgb(var(--gold-light) / <alpha-value>)',
                  muted:'rgb(var(--gold-muted) / <alpha-value>)', dim:'rgb(var(--gold-dim) / <alpha-value>)'
                },
                cream: {
                  DEFAULT:'rgb(var(--cream) / <alpha-value>)', muted:'rgb(var(--cream-muted) / <alpha-value>)',
                  subtle:'rgb(var(--cream-subtle) / <alpha-value>)'
                },
                success:'rgb(var(--success) / <alpha-value>)',
                danger:'rgb(var(--danger) / <alpha-value>)',
              },
              fontFamily: {
                serif: ['"Noto Serif SC"', 'STSong', 'Georgia', 'serif'],
                mono:  ['"JetBrains Mono"', '"Cascadia Code"', 'Consolas', 'monospace'],
                sans:  ['"IBM Plex Sans"', '"Microsoft YaHei UI"', 'sans-serif'],
              },
              animation: { 'fade-in': 'fadeIn 0.4s ease-out both' },
              keyframes: { fadeIn: { '0%': { opacity:'0' }, '100%': { opacity:'1' } } }
            }
          }
        } 
      };
    </script>

    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script>
        window.MathJax = {
            tex: { inlineMath: [['$', '$'], ['\\(', '\\)']], displayMath: [['$$', '$$'], ['\\[', '\\]']] },
            startup: { typeset: false },
            svg: { fontCache: 'global' }
        };
    </script>
    <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/@alpinejs/collapse@3.x.x/dist/cdn.min.js"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/sortablejs@latest/Sortable.min.js"></script>

    <style>
        :root,
        html[data-theme="light"] {
            color-scheme: light;
            --ink-bg: 246 243 236;
            --ink-card: 255 253 249;
            --ink-elevated: 244 240 232;
            --ink-input: 238 233 223;
            --ink-border: 211 202 185;
            --ink-deep: 39 30 17;
            --gold: 157 109 30;
            --gold-light: 181 128 37;
            --gold-muted: 125 94 45;
            --gold-dim: 151 133 101;
            --cream: 43 45 39;
            --cream-muted: 91 91 81;
            --cream-subtle: 125 122 109;
            --success: 50 119 77;
            --danger: 178 67 58;
            --page-glow: rgba(190, 137, 49, 0.13);
            --panel-border: rgba(92, 75, 49, 0.13);
            --panel-shadow: 0 18px 50px rgba(79, 61, 35, 0.08), 0 2px 8px rgba(79, 61, 35, 0.05);
            --terminal: 31 35 31;
        }
        html[data-theme="dark"] {
            color-scheme: dark;
            --ink-bg: 18 21 18;
            --ink-card: 25 29 25;
            --ink-elevated: 30 35 30;
            --ink-input: 36 42 36;
            --ink-border: 55 63 55;
            --ink-deep: 28 22 10;
            --gold: 202 164 92;
            --gold-light: 226 198 139;
            --gold-muted: 164 139 88;
            --gold-dim: 112 100 72;
            --cream: 222 219 204;
            --cream-muted: 166 164 150;
            --cream-subtle: 122 126 114;
            --success: 111 166 121;
            --danger: 204 106 95;
            --page-glow: rgba(202, 164, 92, 0.08);
            --panel-border: rgba(235, 230, 207, 0.055);
            --panel-shadow: 0 20px 55px rgba(0, 0, 0, 0.22), 0 2px 8px rgba(0, 0, 0, 0.18);
            --terminal: 12 15 13;
        }
        * { scrollbar-width: thin; scrollbar-color: rgb(var(--ink-border)) transparent; }
        ::selection { color: rgb(var(--ink-deep)); background: rgb(var(--gold) / 0.42); }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgb(var(--ink-border)); border-radius: 999px; }
        ::-webkit-scrollbar-thumb:hover { background: rgb(var(--gold-muted) / 0.72); }
        html, body { min-height: 100%; background: rgb(var(--ink-bg)); transition: background-color .28s ease, color .28s ease; }
        body { letter-spacing: 0.006em; }
        body::before {
            content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0;
            background:
                radial-gradient(ellipse 70% 55% at 12% -8%, var(--page-glow) 0%, transparent 72%),
                radial-gradient(ellipse 55% 42% at 92% 92%, rgb(var(--success) / 0.045) 0%, transparent 72%);
        }
        body::after {
            content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0; opacity: .32;
            background-image: linear-gradient(rgb(var(--cream) / .018) 1px, transparent 1px), linear-gradient(90deg, rgb(var(--cream) / .018) 1px, transparent 1px);
            background-size: 28px 28px;
            mask-image: linear-gradient(to bottom, black, transparent 72%);
        }
        html[data-theme="light"] body::after { opacity: .55; }
        [x-cloak] { display: none !important; }
        button, input, textarea, select { font: inherit; }
        button:focus-visible, input:focus-visible, textarea:focus-visible, select:focus-visible {
            outline: 2px solid rgb(var(--gold) / .78); outline-offset: 2px;
        }
        .ink-card {
            background: rgb(var(--ink-card) / .9);
            border: 1px solid var(--panel-border);
            box-shadow: var(--panel-shadow);
            backdrop-filter: blur(18px) saturate(115%);
            transition: background-color .28s ease, border-color .28s ease, box-shadow .28s ease;
        }
        .app-header { position: relative; overflow: hidden; }
        .app-header::after {
            content: ''; position: absolute; inset: auto 0 0; height: 1px;
            background: linear-gradient(90deg, transparent, rgb(var(--gold) / .45), transparent);
        }
        .brand-logo { filter: drop-shadow(0 8px 16px rgb(var(--gold) / .08)); }
        .brand-logo .logo-shell { fill: rgb(var(--ink-deep)); }
        .brand-logo .logo-hub-text { fill: rgb(var(--ink-deep)); }
        .drag-ghost { opacity: .28; background: rgb(var(--ink-input)) !important; border: 1px dashed rgb(var(--gold)) !important; }
        .gold-glow:focus-within { box-shadow: 0 0 0 1px rgb(var(--gold) / .42), 0 0 0 4px rgb(var(--gold) / .08); }
        .toggle-track { transition: background-color .25s ease; }
        .toggle-dot { transition: transform .25s cubic-bezier(.34,1.56,.64,1); }
        .theme-toggle {
            display: inline-flex; align-items: center; gap: .55rem; min-height: 40px; padding: .45rem .72rem;
            color: rgb(var(--cream-muted)); background: rgb(var(--ink-input) / .72);
            border: 1px solid var(--panel-border); border-radius: .75rem;
            transition: color .2s ease, background-color .2s ease, border-color .2s ease, transform .2s ease;
        }
        .theme-toggle:hover { color: rgb(var(--cream)); background: rgb(var(--ink-input)); border-color: rgb(var(--gold) / .28); transform: translateY(-1px); }
        .theme-toggle-icon { width: 1rem; height: 1rem; color: rgb(var(--gold)); }
        .primary-action {
            color: rgb(var(--ink-deep)); background: linear-gradient(180deg, rgb(var(--gold-light)), rgb(var(--gold)));
            box-shadow: 0 9px 24px rgb(var(--gold) / .19), inset 0 1px 0 rgb(255 255 255 / .25);
        }
        .primary-action:hover { filter: saturate(1.06) brightness(1.04); box-shadow: 0 12px 30px rgb(var(--gold) / .28); transform: translateY(-1px); }
        .primary-action:active { transform: scale(.97); }
        .secondary-action { box-shadow: inset 0 1px 0 rgb(var(--cream) / .025); }
        .terminal-panel { background: rgb(var(--terminal)); border: 1px solid rgb(var(--cream) / .055); }
        .soft-gold-panel { background: rgb(var(--gold) / .055) !important; }
        .soft-panel { background: rgb(var(--cream) / .025) !important; }
        select option { background-color: rgb(var(--ink-card)); color: rgb(var(--cream)); }
        html[data-theme="light"] [class*="border-white"] { border-color: rgb(75 65 50 / .12) !important; }
        html[data-theme="light"] [class*="hover:border-white"]:hover { border-color: rgb(75 65 50 / .22) !important; }
        .prose mjx-container { outline: none !important; }
        .prose mjx-container svg { max-width: none !important; height: auto !important; display: inline !important; }
        mjx-container:not([display="true"]) { display: inline-block !important; margin: 0 !important; }
        mjx-container[display="true"] { margin: .75em 0 !important; }
        html[data-theme="light"] .prose-invert {
            --tw-prose-body: rgb(var(--cream)); --tw-prose-headings: rgb(var(--cream));
            --tw-prose-lead: rgb(var(--cream-muted)); --tw-prose-links: rgb(var(--gold));
            --tw-prose-bold: rgb(var(--cream)); --tw-prose-counters: rgb(var(--cream-subtle));
            --tw-prose-bullets: rgb(var(--gold-muted)); --tw-prose-hr: rgb(var(--ink-border));
            --tw-prose-quotes: rgb(var(--cream)); --tw-prose-quote-borders: rgb(var(--gold));
            --tw-prose-captions: rgb(var(--cream-subtle)); --tw-prose-code: rgb(var(--cream));
            --tw-prose-pre-code: rgb(var(--cream)); --tw-prose-pre-bg: rgb(var(--ink-elevated));
            --tw-prose-th-borders: rgb(var(--ink-border)); --tw-prose-td-borders: rgb(var(--ink-border));
        }
        .workspace-shell { height: calc(100vh - 140px); }
        @media (max-width: 1023px) {
            .app-header { align-items: flex-start; }
            .header-actions { width: 100%; justify-content: flex-end; }
            .workspace-shell { height: auto; flex-direction: column; }
            .problem-sidebar { width: 100% !important; max-height: 280px; }
            .workspace-panel { min-height: 720px; }
        }
        @media (max-width: 640px) {
            .header-actions { justify-content: flex-start; }
            .workspace-panel { min-height: 760px; padding: 1rem; }
            .app-header > div:first-child { align-items: flex-start; gap: .8rem; }
            .brand-logo { width: 8.4rem; height: auto; margin-top: .15rem; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; transition-duration: .01ms !important; }
        }
    </style>
</head>
<body class="min-h-screen bg-ink-bg text-cream antialiased relative">
    <div x-data="probhub()" x-init="initApp()" @input="autoSave()" class="max-w-[1500px] mx-auto px-3 sm:px-5 lg:px-8 py-4 lg:py-6 relative z-10" x-cloak>

        <div class="ink-card app-header rounded-2xl p-4 lg:p-5 mb-5 flex flex-wrap justify-between items-center gap-4">
            <div class="flex items-center gap-5">
                <svg class="brand-logo h-11 lg:h-12 w-auto select-none shrink-0" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 324 90">
                    <style>.logo-text { font-family: "New Computer Modern Mono", "Courier New", monospace; font-weight: bold; font-size: 64px; }</style>
                    <rect class="logo-shell" width="324" height="90" rx="16" />
                    <text x="22" y="64" class="logo-text" fill="#E53935">p</text>
                    <text x="58" y="64" class="logo-text" fill="#E53935">r</text>
                    <text x="94" y="64" class="logo-text" fill="#E53935">o</text>
                    <text x="130" y="64" class="logo-text" fill="#1E88E5">b</text>
                    <rect x="182" y="14" width="130" height="62" rx="10" fill="#FFA31A" />
                    <text x="192" y="64" class="logo-text logo-hub-text">h</text>
                    <text x="228" y="64" class="logo-text logo-hub-text">u</text>
                    <text x="264" y="64" class="logo-text logo-hub-text">b</text>
                </svg>
                <div class="flex flex-col gap-1">
                    <span class="text-[11px] tracking-[0.2em] uppercase text-cream-muted font-medium">Typesetting Console</span>
                    <div class="flex items-center gap-3 flex-wrap">
                        <span class="text-sm text-cream-subtle">当前排版集</span>
                        <div class="relative flex items-center" x-show="subtitles.length > 0">
                            <select x-model="currentSubtitle" @input.stop @change.stop="switchSubtitle()"
                                    class="bg-ink-input text-gold font-mono text-[13px] border border-white/[0.02] rounded-md pl-2.5 pr-8 py-1 outline-none focus:border-gold/40 cursor-pointer appearance-none transition-colors shadow-sm">
                                <template x-for="sub in subtitles" :key="sub">
                                    <option :value="sub" x-text="sub"></option>
                                </template>
                            </select>
                            <svg class="w-3.5 h-3.5 text-gold/60 absolute right-2.5 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
                        </div>
                        <span x-show="subtitles.length === 0" class="text-[13px] text-danger font-mono bg-danger/10 px-2 py-0.5 rounded border border-danger/20">未检测到排版集</span>
                        <div class="inline-flex items-center gap-1 p-1 rounded-lg bg-ink-input/70 border border-white/[0.02]">
                            <button @click="activePage = 'layout'"
                                    class="px-3 py-1.5 rounded-md text-[12px] font-medium transition-colors"
                                    :class="activePage === 'layout' ? 'bg-gold/12 text-gold-light' : 'text-cream-subtle hover:text-cream'">
                                组卷排版
                            </button>
                            <button @click="activePage = 'sandbox'; refreshSandboxInfo()"
                                    class="px-3 py-1.5 rounded-md text-[12px] font-medium transition-colors"
                                    :class="activePage === 'sandbox' ? 'bg-gold/12 text-gold-light' : 'text-cream-subtle hover:text-cream'">
                                沙箱评测
                            </button>
                        </div>
                    </div>
                </div>
            </div>

            <div class="header-actions flex flex-wrap items-center gap-2.5">
                <button @click="selectedIdx = null" x-show="selectedIdx !== null"
                        class="flex items-center gap-1.5 text-[12px] text-cream-subtle hover:text-cream transition-colors duration-200 group">
                    <svg class="w-3.5 h-3.5 transition-transform duration-200 group-hover:-translate-x-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 19l-7-7 7-7"/></svg>
                    <span class="font-medium">概览</span>
                </button>
                <span class="text-[12px] font-medium flex items-center gap-1.5 transition-colors duration-300 select-none min-w-[80px]"
                      :class="saveStatus === 'saved' ? 'text-success' : (saveStatus === 'error' ? 'text-danger' : (saveStatus === 'saving' ? 'text-cream-subtle' : 'text-cream-subtle/60'))">
                    <template x-if="saveStatus === 'saved'"><span>✓ 已保存</span></template>
                    <template x-if="saveStatus === 'saving'"><span class="animate-pulse">● 保存中</span></template>
                    <template x-if="saveStatus === 'error'"><span class="cursor-pointer" @click.stop="doSave()">⚠ 点击重试</span></template>
                    <template x-if="!saveStatus"><span>● 就绪</span></template>
                </span>
                <button type="button" class="theme-toggle" @click="toggleTheme()"
                        :aria-label="theme === 'dark' ? '切换到明亮模式' : '切换到暗夜模式'"
                        :title="theme === 'dark' ? '切换到明亮模式' : '切换到护眼暗夜模式'">
                    <svg x-show="theme === 'dark'" class="theme-toggle-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M12 3v1.5M12 19.5V21M3 12h1.5M19.5 12H21M5.64 5.64l1.06 1.06M17.3 17.3l1.06 1.06M18.36 5.64 17.3 6.7M6.7 17.3l-1.06 1.06M16.5 12a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0Z"/></svg>
                    <svg x-show="theme === 'light'" class="theme-toggle-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.8" d="M20.5 15.5A8.5 8.5 0 0 1 8.5 3.5a8.5 8.5 0 1 0 12 12Z"/></svg>
                    <span class="text-[12px] font-medium" x-text="theme === 'dark' ? 'Light' : 'Dark'"></span>
                </button>
                <button @click="compilePDF()" class="primary-action px-5 py-2.5 text-[13px] font-semibold rounded-lg transition-all duration-200 disabled:opacity-50 disabled:shadow-none" :disabled="isCompiling || !currentSubtitle">
                    <span class="flex items-center gap-1.5">
                        <span x-show="!isCompiling">📄 编译全卷</span>
                        <span x-show="isCompiling" class="animate-pulse">⏳ 编译中...</span>
                    </span>
                </button>
                <button @click="distributePDFs()" class="secondary-action px-4 py-2.5 text-[13px] font-medium rounded-lg transition-all duration-200 bg-ink-elevated border border-white/[0.03] text-cream-muted hover:text-cream hover:border-white/8 hover:bg-ink-input active:scale-[0.97] disabled:opacity-40" :disabled="isDistributing || !currentSubtitle">
                    <span class="flex items-center gap-1.5">
                        <span x-show="!isDistributing">📦 分发 PDF</span>
                        <span x-show="isDistributing" class="animate-pulse">⏳ 分发中...</span>
                    </span>
                </button>
            </div>
        </div>

        <div x-show="toast.show" x-transition.opacity.duration.300ms class="fixed top-6 right-6 z-50 flex items-center gap-2 max-w-xl px-5 py-3 rounded-xl text-sm font-medium leading-relaxed shadow-2xl border backdrop-blur-md" :class="toast.isError ? 'bg-danger/90 border-red-500/30 text-white' : 'bg-success/90 border-green-500/30 text-white'">
            <span class="whitespace-normal" x-text="toast.msg"></span>
        </div>

        <div class="workspace-shell flex gap-5">

            <div class="problem-sidebar w-[220px] shrink-0 ink-card rounded-2xl p-4 flex flex-col overflow-hidden">
                <div class="flex items-center justify-between mb-4 px-1 shrink-0">
                    <h2 class="font-serif text-[15px] font-semibold text-cream tracking-wide">题目列表</h2>
                    <span class="text-[11px] text-cream-subtle font-mono" x-text="problems.length + ' 题'"></span>
                </div>
                <div id="sortable-list" class="space-y-1.5 flex-1 overflow-y-auto pr-0.5">
                    <template x-for="(prob, index) in problems" :key="prob.problem.display_name + index">
                        <div @click="selectProb(index)" :class="selectedIdx === index ? 'bg-gold/10 border-gold/40 text-cream' : 'border-transparent hover:border-white/[0.03] hover:bg-ink-elevated/50 text-cream-muted'" class="flex flex-wrap items-center gap-x-2.5 gap-y-1 px-3 py-2.5 rounded-lg cursor-move border transition-all duration-150 group">
                            <span class="text-[11px] font-mono shrink-0 opacity-50 w-4 text-center" :class="selectedIdx === index ? 'text-gold opacity-80' : ''" x-text="String.fromCharCode(65 + index)"></span>
                            <span class="w-1.5 h-4 rounded-full shrink-0 transition-transform duration-200" :class="selectedIdx === index ? 'scale-125' : ''" :style="'background:' + getDifficultyInfo(index).color"></span>
                            <span class="text-[13px] font-medium truncate min-w-0 flex-1" x-text="prob.problem.display_name"></span>
                            <span class="inline-flex flex-wrap items-center gap-1 shrink-0">
                                <template x-for="(tag, ti) in getTags(index)" :key="ti">
                                    <span
                                          class="text-[9px] px-1.5 py-0.5 rounded leading-none font-medium transition-all duration-150"
                                          :class="selectedIdx === index ? 'bg-gold/12 border border-gold/25 text-gold-light' : 'bg-ink-input/80 border border-white/[0.03] text-cream-muted'"
                                          x-text="tag"></span>
                                </template>
                            </span>
                        </div>
                    </template>
                    <div x-show="problems.length === 0 && currentSubtitle" class="text-center text-sm text-cream-subtle mt-10">
                        当前排版集为空
                    </div>
                </div>
            </div>

            <div x-show="activePage === 'layout'" class="workspace-panel flex-1 ink-card rounded-2xl p-6 overflow-y-auto relative">
                <div x-show="selectedIdx === null" class="absolute inset-0 overflow-y-auto">
                    <!-- Empty set: no problems loaded yet -->
                    <div x-show="problems.length === 0" class="absolute inset-0 flex flex-col items-center justify-center">
                        <div class="w-20 h-20 rounded-2xl bg-ink-input/40 flex items-center justify-center mb-5">
                            <svg class="w-8 h-8 text-cream-subtle/40" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>
                        </div>
                        <p class="font-serif text-[15px] text-cream-muted mb-1.5">选择一道题目开始编排</p>
                        <p class="text-xs text-cream-subtle">在左侧列表中点击题目，即可编辑题面、难度与样例</p>
                    </div>

                    <!-- Dashboard: problems exist but none selected -->
                    <div x-show="problems.length > 0" class="p-6 space-y-8">
                        <div class="flex items-center gap-4">
                            <div class="w-14 h-14 rounded-2xl bg-ink-input/40 flex items-center justify-center shrink-0">
                                <svg class="w-6 h-6 text-cream-subtle/50" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
                            </div>
                            <div>
                                <h2 class="font-serif text-[16px] font-semibold text-cream">排版集概览</h2>
                                <p class="text-xs text-cream-subtle mt-0.5">
                                    <span x-text="currentSubtitle"></span> · <span x-text="problems.length + ' 题'"></span>
                                </p>
                            </div>
                        </div>

                        <!-- Cover Settings -->
                        <div @input.stop="autoSaveCover()">
                            <div class="flex items-center justify-between mb-4">
                                <div class="flex items-center gap-2">
                                    <div class="w-1 h-3.5 rounded-full bg-gold"></div>
                                    <h3 class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">封面设置 <span class="text-[9px] text-cream-subtle font-normal tracking-normal normal-case">（编译后可预览 当心眼睛（）</span></h3>
                                </div>
                            </div>
                            <div class="grid grid-cols-2 gap-3">
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">比赛标题</label>
                                    <input type="text" x-model="coverConfig.title" class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors">
                                </div>
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">副标题</label>
                                    <input type="text" x-model="coverConfig.subtitle" class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors">
                                </div>
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">作者</label>
                                    <input type="text" x-model="coverConfig.author" class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors">
                                </div>
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">日期</label>
                                    <input type="text" x-model="coverConfig.date" class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors">
                                </div>
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">校徽文件</label>
                                    <input type="text" x-model="coverConfig.logo" class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors font-mono">
                                </div>
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">校徽宽度</label>
                                    <input type="text" x-model="coverConfig.logo_width" class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors font-mono">
                                </div>
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">校徽上方间距 <span class="text-cream-subtle">（可为负数）</span></label>
                                    <input type="text" x-model="coverConfig.logo_space_above" class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors font-mono">
                                </div>
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">校徽下方间距</label>
                                    <input type="text" x-model="coverConfig.logo_space_below" class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors font-mono">
                                </div>
                            </div>
                            <!-- Cover preview thumbnail -->
                            <template x-if="currentSubtitle && pdfPages.length > 0">
                                <div class="mt-3" data-testid="cover-preview">
                                    <p class="text-[10px] font-medium text-cream-subtle mb-2">封面预览</p>
                                    <div class="rounded-lg overflow-hidden border border-white/[0.02] bg-ink-bg relative">
                                        <img :src="'/api/pdf-page/' + encodeURIComponent(currentSubtitle) + '/0?t=' + pdfRefresh"
                                             class="w-full block" :style="theme === 'light' ? 'filter:none' : 'filter:brightness(0.92) contrast(0.95)'">
                                        <div x-show="theme === 'dark'" class="absolute inset-0 bg-black/20 pointer-events-none"></div>
                                    </div>
                                </div>
                            </template>
                        </div>

                        <!-- Difficulty Distribution -->
                        <div>
                            <div class="flex items-center gap-2 mb-4">
                                <div class="w-1 h-3.5 rounded-full bg-gold"></div>
                                <h3 class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">难度分布</h3>
                            </div>
                            <div class="space-y-1.5">
                                <template x-for="(level, li) in difficultyLevels" :key="level.label">
                                    <div class="flex items-center gap-2.5">
                                        <span class="text-[10px] font-mono w-[72px] text-right shrink-0" :style="'color:' + level.color" x-text="level.label"></span>
                                        <div class="flex-1 h-5 rounded-md bg-ink-input overflow-hidden">
                                            <div class="h-full rounded-md transition-all duration-700 ease-out"
                                                 :style="'width:' + (getDifficultyStats()[li] / Math.max(1, problems.length) * 100) + '%; background:' + level.color"></div>
                                        </div>
                                        <span class="text-[11px] font-mono text-cream-subtle w-4 text-right shrink-0 font-medium" x-text="getDifficultyStats()[li]"></span>
                                    </div>
                                </template>
                            </div>
                        </div>

                        <!-- Tag Cloud -->
                        <div x-show="getAllTags().length > 0">
                            <div class="flex items-center gap-2 mb-4">
                                <div class="w-1 h-3.5 rounded-full bg-gold"></div>
                                <h3 class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">算法标签</h3>
                                <span class="text-[10px] font-mono text-cream-subtle/60" x-text="getAllTags().length + ' 种'"></span>
                            </div>
                            <div class="flex flex-wrap gap-1.5">
                                <template x-for="tag in getAllTags()" :key="tag">
                                    <span class="text-[10px] px-2.5 py-1 rounded-md font-medium leading-none bg-ink-elevated border border-white/[0.03] text-cream-muted select-none" x-text="tag"></span>
                                </template>
                            </div>
                        </div>

                        <p class="text-[12px] text-cream-subtle/50 text-center pt-4">点击左侧题目开始编辑</p>
                    </div>
                </div>

                <template x-if="problems[selectedIdx]">
                    <div class="space-y-8 animate-fade-in">

                        <div class="grid grid-cols-[1fr_auto] gap-6 items-end">
                            <div>
                                <label class="block text-[11px] font-medium tracking-wide text-cream-muted uppercase mb-1.5">Display Name</label>
                                <input type="text" x-model="problems[selectedIdx].problem.display_name" class="w-full px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg text-[14px] font-medium text-cream focus:border-gold/40 focus:outline-none transition-colors">
                            </div>
                            <div class="flex items-end pb-2">
                                <label class="flex items-center gap-2.5 cursor-pointer select-none">
                                    <div class="relative w-9 h-5 rounded-full toggle-track transition-colors" :class="hasQuote() ? 'bg-gold' : 'bg-ink-border'">
                                        <div class="toggle-dot absolute top-0.5 w-4 h-4 rounded-full bg-white shadow-md" :class="hasQuote() ? 'translate-x-[18px]' : 'translate-x-[2px]'"></div>
                                    </div>
                                    <input type="checkbox" class="sr-only" :checked="hasQuote()" @change="toggleQuote($event.target.checked)">
                                    <span class="text-[12px] font-medium" :class="hasQuote() ? 'text-gold' : 'text-cream-subtle'">启用题面引言 (Quote)</span>
                                </label>
                            </div>
                        </div>

                        <!-- Tags Editor -->
                        <div class="pt-5 mt-2 border-t border-white/[0.02]">
                            <div class="flex items-center justify-between mb-3">
                                <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">Tags</label>
                                <span class="text-[10px] font-mono text-cream-subtle/60" x-text="getTags(selectedIdx).length + ' tags'"></span>
                            </div>
                            <div class="flex flex-wrap items-center gap-1.5">
                                <template x-for="(tag, ti) in getTags(selectedIdx)" :key="ti">
                                    <span class="flex items-center gap-1 text-[10px] px-2 py-1 rounded-md leading-none font-medium bg-gold/12 border border-gold/20 text-gold-light group/tag transition-colors">
                                        <span x-text="tag"></span>
                                        <button @click="removeTag(selectedIdx, ti)" class="text-gold-light/50 hover:text-danger transition-colors leading-none">&times;</button>
                                    </span>
                                </template>
                                <div class="relative flex items-center">
                                    <input type="text" x-ref="tagInput" x-model="tagDraft"
                                           @keydown.enter.prevent="commitTag()"
                                           @keydown.,.prevent="commitTag()"
                                           @keydown.backspace="if(!tagDraft && getTags(selectedIdx).length) removeTag(selectedIdx, getTags(selectedIdx).length - 1)"
                                           class="w-24 px-2.5 py-1 bg-transparent border border-dashed border-white/[0.02] rounded-md text-[10px] text-cream placeholder-cream-subtle/30 focus:border-gold/30 focus:outline-none transition-colors"
                                           placeholder="添加标签">
                                </div>
                            </div>
                        </div>

                        <!-- Limits Editor -->
                        <div class="pt-5 mt-2 border-t border-white/[0.02]">
                            <div class="flex items-center justify-between mb-3">
                                <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">Limits</label>
                                <span class="text-[10px] font-mono text-cream-subtle/60">
                                    <span x-text="getTimeLimit(selectedIdx)"></span>s /
                                    <span x-text="getMemoryLimit(selectedIdx)"></span>MB
                                </span>
                            </div>
                            <div class="grid grid-cols-2 gap-3">
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">时间限制</label>
                                    <div class="flex items-center gap-2">
                                        <input type="number" min="0.1" step="0.1"
                                               :value="getTimeLimit(selectedIdx)"
                                               @input.stop="setTimeLimit(selectedIdx, $event.target.value)"
                                               class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors font-mono">
                                        <span class="text-[11px] text-cream-subtle font-mono">s</span>
                                    </div>
                                </div>
                                <div>
                                    <label class="block text-[10px] font-medium text-cream-subtle mb-1">内存限制</label>
                                    <div class="flex items-center gap-2">
                                        <input type="number" min="1" step="1"
                                               :value="getMemoryLimit(selectedIdx)"
                                               @input.stop="setMemoryLimit(selectedIdx, $event.target.value)"
                                               class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg text-[13px] text-cream focus:border-gold/40 focus:outline-none transition-colors font-mono">
                                        <span class="text-[11px] text-cream-subtle font-mono">MB</span>
                                    </div>
                                </div>
                            </div>
                        </div>

                        <!-- Difficulty Slider -->
                        <div class="pt-5 mt-2 border-t border-white/[0.02]">
                            <div class="flex items-center justify-between mb-3">
                                <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">Difficulty</label>
                                <span class="text-[12px] font-semibold px-2.5 py-0.5 rounded font-mono transition-all duration-300"
                                      :style="'background:' + getDifficultyInfo(selectedIdx).bg + '; color:' + getDifficultyInfo(selectedIdx).color"
                                      x-text="getDifficultyInfo(selectedIdx).label"></span>
                            </div>
                            <!-- Stepped Track -->
                            <div class="relative h-12 flex items-center cursor-pointer select-none"
                                 x-init="trackWidth = $el.offsetWidth; new ResizeObserver(() => trackWidth = $el.offsetWidth).observe($el)"
                                 @click="setDifficultyFromTrack(selectedIdx, $event)">
                                <div class="absolute inset-x-0 h-1.5 rounded-full bg-ink-border"></div>
                                <div class="absolute left-0 h-1.5 rounded-full overflow-hidden transition-all duration-300"
                                     :style="'width:' + (getDifficulty(selectedIdx) / 5 * 100) + '%'">
                                    <div class="flex h-full" :style="'width:' + trackWidth + 'px'">
                                        <div class="flex-1 h-full" style="background:linear-gradient(90deg,#9b7ec4,#5a8ec0)"></div>
                                        <div class="flex-1 h-full" style="background:linear-gradient(90deg,#5a8ec0,#6b9b6a)"></div>
                                        <div class="flex-1 h-full" style="background:linear-gradient(90deg,#6b9b6a,#c8a050)"></div>
                                        <div class="flex-1 h-full" style="background:linear-gradient(90deg,#c8a050,#e08840)"></div>
                                        <div class="flex-1 h-full" style="background:linear-gradient(90deg,#e08840,#e05555)"></div>
                                    </div>
                                </div>
                                <template x-for="(level, li) in difficultyLevels" :key="level.label">
                                    <div class="absolute w-3.5 h-3.5 rounded-full transition-all duration-300 z-10"
                                         :style="'left:calc(' + (li / 5 * 100) + '% - 7px); background:' + (getDifficulty(selectedIdx) >= li ? level.color : 'rgb(var(--ink-input))') + '; border: 2px solid ' + (getDifficulty(selectedIdx) >= li ? level.color : 'rgb(var(--ink-border))') + ';' + (getDifficulty(selectedIdx) === li ? 'transform: scale(1.7); box-shadow: 0 0 12px ' + level.color + '99;' : '')"
                                         @click.stop="setDifficulty(selectedIdx, li)"></div>
                                </template>
                            </div>
                            <!-- Labels -->
                            <div class="flex justify-between mt-1">
                                <template x-for="(level, li) in difficultyLevels" :key="level.label">
                                    <span class="text-[9px] font-medium transition-colors duration-300 select-none"
                                          :style="getDifficulty(selectedIdx) === li ? 'color:' + level.color : 'color:rgb(var(--cream-subtle))'"
                                          x-text="level.label"></span>
                                </template>
                            </div>
                        </div>

                        <!-- Quote expandable section -->
                        <template x-if="hasQuote()">
                            <div x-show="true" x-collapse>
                                <div class="soft-gold-panel p-4 rounded-xl space-y-4 border-l-2 border-gold/30">
                                    <div>
                                        <label class="block text-[11px] font-medium tracking-wide text-cream-muted uppercase mb-1.5">引言内容</label>
                                        <textarea x-model="problems[selectedIdx].statement.quote.text" rows="3"
                                                  class="w-full px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg
                                                         text-[13px] text-cream placeholder-cream-subtle/40 leading-relaxed
                                                         focus:border-gold/40 focus:outline-none transition-colors resize-none"
                                                  placeholder="这是出题人最喜欢的一段话..."></textarea>
                                    </div>
                                    <div>
                                        <label class="block text-[11px] font-medium tracking-wide text-cream-muted uppercase mb-1.5">出处</label>
                                        <input type="text" x-model="problems[selectedIdx].statement.quote.source"
                                               class="w-full px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg
                                                      text-[13px] text-cream placeholder-cream-subtle/40
                                                      focus:border-gold/40 focus:outline-none transition-colors"
                                               placeholder="——《某某书籍》">
                                    </div>
                                </div>
                            </div>
                        </template>

                        <div class="space-y-6 pt-4 border-t border-white/[0.02]">
                            <div>
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-2">
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full bg-gold"></div> Description</label>
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full opacity-0"></div> Live Preview</label>
                                </div>
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <textarea x-model="problems[selectedIdx].statement.description" rows="8" class="w-full h-56 px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg font-mono text-[13px] text-cream placeholder-cream-subtle/40 leading-relaxed focus:border-gold/40 focus:outline-none transition-colors resize-none"></textarea>
                                    <div class="w-full h-56 px-4 py-3 bg-ink-elevated border border-white/[0.02] rounded-lg overflow-y-auto prose prose-invert max-w-none text-[13.5px] leading-relaxed prose-p:my-1.5 prose-p:leading-normal prose-ul:my-1.5 prose-li:my-0.5 prose-pre:bg-ink-card prose-pre:my-2 prose-a:text-gold" x-effect="renderMath($refs.descPreview, problems[selectedIdx]?.statement?.description)" x-ref="descPreview"></div>
                                </div>
                            </div>

                            <div>
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-2">
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full bg-gold"></div> Input Format</label>
                                </div>
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <textarea x-model="problems[selectedIdx].statement.input" class="w-full h-32 px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg font-mono text-[13px] text-cream leading-relaxed focus:border-gold/40 focus:outline-none transition-colors resize-none"></textarea>
                                    <div class="w-full h-32 px-4 py-3 bg-ink-elevated border border-white/[0.02] rounded-lg overflow-y-auto prose prose-invert max-w-none text-[13.5px] leading-relaxed prose-p:my-1.5 prose-p:leading-normal prose-ul:my-1.5 prose-li:my-0.5 prose-pre:bg-ink-card prose-pre:my-2 prose-a:text-gold" x-effect="renderMath($refs.inputPreview, problems[selectedIdx]?.statement?.input)" x-ref="inputPreview"></div>
                                </div>
                            </div>

                            <div>
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-2">
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full bg-gold"></div> Output Format</label>
                                </div>
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <textarea x-model="problems[selectedIdx].statement.output" class="w-full h-32 px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg font-mono text-[13px] text-cream leading-relaxed focus:border-gold/40 focus:outline-none transition-colors resize-none"></textarea>
                                    <div class="w-full h-32 px-4 py-3 bg-ink-elevated border border-white/[0.02] rounded-lg overflow-y-auto prose prose-invert max-w-none text-[13.5px] leading-relaxed prose-p:my-1.5 prose-p:leading-normal prose-ul:my-1.5 prose-li:my-0.5 prose-pre:bg-ink-card prose-pre:my-2 prose-a:text-gold" x-effect="renderMath($refs.outputPreview, problems[selectedIdx]?.statement?.output)" x-ref="outputPreview"></div>
                                </div>
                            </div>

                            <!-- Samples Section -->
                            <div>
                                <div class="flex items-center justify-between mb-3">
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full bg-gold"></div> Samples</label>
                                    <button @click="addSample()" class="px-3 py-1.5 text-[11px] font-medium rounded-md transition-all duration-150 border border-gold/30 text-gold hover:bg-gold/10 active:scale-[0.97]">
                                        + 添加样例
                                    </button>
                                </div>
                                <template x-if="!problems[selectedIdx].problem.samples || problems[selectedIdx].problem.samples.length === 0">
                                    <p class="text-[12px] text-cream-subtle italic py-6 text-center border border-dashed border-white/[0.03] rounded-lg">暂无样例，点击上方按钮添加</p>
                                </template>
                                <template x-if="problems[selectedIdx].problem.samples && problems[selectedIdx].problem.samples.length > 0">
                                    <div class="space-y-3">
                                        <template x-for="(sample, si) in problems[selectedIdx].problem.samples" :key="si">
                                            <div class="soft-panel p-4 rounded-xl border border-white/[0.02]">
                                                <div class="flex items-center justify-between mb-2.5">
                                                    <span class="text-[11px] font-mono text-gold tracking-wide" x-text="'样例 #' + (si + 1)"></span>
                                                    <button @click="removeSample(si)" class="text-[11px] text-cream-subtle hover:text-danger transition-colors px-2 py-0.5 rounded hover:bg-danger/10" x-show="problems[selectedIdx].problem.samples.length > 1">✕ 移除</button>
                                                </div>
                                                <div class="grid grid-cols-2 gap-3">
                                                    <div>
                                                        <label class="block text-[10px] font-medium tracking-wide text-cream-muted uppercase mb-1">Input</label>
                                                        <textarea x-model="sample.input" rows="3"
                                                                  class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg font-mono text-[12px] text-cream leading-relaxed focus:border-gold/40 focus:outline-none transition-colors resize-none"
                                                                  placeholder="3 5 7"></textarea>
                                                    </div>
                                                    <div>
                                                        <label class="block text-[10px] font-medium tracking-wide text-cream-muted uppercase mb-1">Output</label>
                                                        <textarea x-model="sample.output" rows="3"
                                                                  class="w-full px-3 py-2 bg-ink-input border border-white/[0.03] rounded-lg font-mono text-[12px] text-cream leading-relaxed focus:border-gold/40 focus:outline-none transition-colors resize-none"
                                                                  placeholder="2"></textarea>
                                                    </div>
                                                </div>
                                            </div>
                                        </template>
                                    </div>
                                </template>
                            </div>

                            <div>
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-2">
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full bg-gold"></div> Notes & Constraints</label>
                                </div>
                                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                                    <textarea x-model="problems[selectedIdx].statement.notes" class="w-full h-32 px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg font-mono text-[13px] text-cream leading-relaxed focus:border-gold/40 focus:outline-none transition-colors resize-none"></textarea>
                                    <div class="w-full h-32 px-4 py-3 bg-ink-elevated border border-white/[0.02] rounded-lg overflow-y-auto prose prose-invert max-w-none text-[13.5px] leading-relaxed prose-p:my-1.5 prose-p:leading-normal prose-ul:my-1.5 prose-li:my-0.5 prose-pre:bg-ink-card prose-pre:my-2 prose-a:text-gold" x-effect="renderMath($refs.notesPreview, problems[selectedIdx]?.statement?.notes)" x-ref="notesPreview"></div>
                                </div>
                            </div>

                        </div>
                    </div>
                </template>
            </div>

            <div x-show="activePage === 'sandbox'" class="workspace-panel flex-1 ink-card rounded-2xl p-6 overflow-y-auto relative">
                <div class="space-y-6 animate-fade-in">
                    <div class="flex items-start justify-between gap-4">
                        <div>
                            <div class="flex items-center gap-2 mb-2">
                                <div class="w-1 h-4 rounded-full bg-gold"></div>
                                <h2 class="font-serif text-[18px] font-semibold text-cream">沙箱评测控制台</h2>
                            </div>
                            <p class="text-xs text-cream-subtle">
                                <template x-if="selectedIdx !== null && problems[selectedIdx]">
                                    <span>
                                        当前题目
                                        <span class="font-mono text-gold" x-text="String.fromCharCode(65 + selectedIdx)"></span>
                                        · <span x-text="problems[selectedIdx].problem.display_name"></span>
                                        <span x-show="sandboxLastRunAt" class="ml-2 text-cream-subtle/70">
                                            上次运行 <span class="font-mono" x-text="sandboxLastRunAt"></span>
                                        </span>
                                    </span>
                                </template>
                                <template x-if="selectedIdx === null">
                                    <span>请先在左侧选择一道题目。</span>
                                </template>
                            </p>
                        </div>
                        <button @click="runSandbox()"
                                class="primary-action px-5 py-2.5 text-[13px] font-semibold rounded-lg transition-all duration-200 disabled:opacity-40 disabled:shadow-none"
                                :disabled="sandboxRunning || !sandboxInfo || !sandboxInfo.runnable">
                            <span x-show="!sandboxRunning">▶ 运行沙箱评测</span>
                            <span x-show="sandboxRunning" class="animate-pulse">● 评测中...</span>
                        </button>
                    </div>

                    <div x-show="!sandboxInfo || !sandboxInfo.matched" class="rounded-xl border border-white/[0.02] bg-ink-input/40 p-5 text-sm text-cream-subtle">
                        <p x-show="selectedIdx === null">选择左侧题目后，这里会显示目录、数据和代码状态。</p>
                        <p x-show="selectedIdx !== null && sandboxInfo && !sandboxInfo.matched">未找到匹配的题目目录，请检查该题 `meta.json` 的 `display_name`。</p>
                    </div>

                    <div x-show="sandboxInfo && sandboxInfo.matched" class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-3">
                        <div class="rounded-xl bg-ink-input/45 border border-white/[0.02] p-4">
                            <p class="text-[10px] uppercase tracking-wide text-cream-subtle mb-1">Problem Dir</p>
                            <p class="font-mono text-[13px] text-cream truncate" x-text="sandboxInfo?.dir || '-'"></p>
                        </div>
                        <div class="rounded-xl bg-ink-input/45 border border-white/[0.02] p-4">
                            <p class="text-[10px] uppercase tracking-wide text-cream-subtle mb-1">Test Data</p>
                            <p class="font-mono text-[13px] text-cream"><span x-text="sandboxInfo?.data_count || 0"></span> cases</p>
                            <p class="text-[10px] text-cream-subtle mt-1">
                                <span x-text="sandboxInfo?.sample_count || 0"></span> sample /
                                <span x-text="sandboxInfo?.secret_count || 0"></span> secret
                            </p>
                        </div>
                        <div class="rounded-xl bg-ink-input/45 border border-white/[0.02] p-4">
                            <p class="text-[10px] uppercase tracking-wide text-cream-subtle mb-1">Limits</p>
                            <p class="font-mono text-[13px] text-cream">
                                <span x-text="sandboxInfo?.limits?.time || 1"></span>s /
                                <span x-text="sandboxInfo?.limits?.memory || 256"></span>MB
                            </p>
                        </div>
                        <div class="rounded-xl bg-ink-input/45 border border-white/[0.02] p-4">
                            <p class="text-[10px] uppercase tracking-wide text-cream-subtle mb-1">Validator</p>
                            <p class="font-mono text-[13px]" :class="sandboxInfo?.files?.validator ? 'text-success' : 'text-cream-subtle'" x-text="sandboxInfo?.files?.validator || 'missing'"></p>
                        </div>
                        <div class="rounded-xl bg-ink-input/45 border border-white/[0.02] p-4">
                            <p class="text-[10px] uppercase tracking-wide text-cream-subtle mb-1">Solutions</p>
                            <p class="font-mono text-[13px] text-cream">
                                <span x-text="(sandboxInfo?.files?.std || []).length"></span> std /
                                <span x-text="(sandboxInfo?.files?.brute || []).length"></span> brute /
                                <span x-text="(sandboxInfo?.files?.wrong || []).length"></span> wrong
                            </p>
                        </div>
                    </div>

                    <div x-show="sandboxInfo && sandboxInfo.runnable" class="rounded-xl border border-gold/20 bg-gold/[0.04] p-5 space-y-4">
                        <div class="flex items-start justify-between gap-4">
                            <div>
                                <div class="flex items-center gap-2">
                                    <h3 class="text-[13px] font-semibold text-cream">临时提交评测</h3>
                                    <span class="px-2 py-0.5 rounded-full bg-success/10 text-success text-[10px] font-mono">isolated</span>
                                </div>
                                <p class="mt-1 text-[11px] text-cream-subtle">上传源码只会进入 <span class="font-mono text-gold">.probhub/submissions/&lt;task-id&gt;</span> 临时目录；不会覆盖题目原有 <span class="font-mono">code/</span> 文件。</p>
                            </div>
                            <span x-show="submissionLastRunAt" class="text-[10px] font-mono text-cream-subtle" x-text="submissionLastRunAt"></span>
                        </div>
                        <div class="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_auto] gap-3 items-center">
                            <label class="flex items-center gap-3 px-4 py-3 rounded-lg border border-dashed border-white/10 bg-ink-input/50 cursor-pointer hover:border-gold/35 transition-colors">
                                <input x-ref="submissionFile" type="file" accept=".cpp,text/x-c++src" class="hidden" @change="handleSubmissionFile($event)">
                                <span class="text-gold">＋</span>
                                <span class="min-w-0">
                                    <span class="block text-[12px] text-cream truncate" x-text="submissionFilename || '选择 UTF-8 C++ 源码（最大 1 MiB）'"></span>
                                    <span class="block text-[10px] text-cream-subtle mt-0.5">仅接受单个 .cpp 文件</span>
                                </span>
                            </label>
                            <div class="flex items-center gap-2">
                                <button @click="cancelSubmission()" x-show="submissionRunning"
                                        class="px-4 py-3 rounded-lg text-[12px] font-semibold border border-danger/30 text-danger hover:bg-danger/10 transition-colors">取消</button>
                                <button @click="runSubmission()"
                                        :disabled="submissionRunning || !submissionFilename"
                                        class="px-5 py-3 rounded-lg text-[12px] font-semibold bg-gold text-ink-deep hover:bg-gold-light disabled:opacity-40 transition-colors">
                                    <span x-show="!submissionRunning">上传并评测</span>
                                    <span x-show="submissionRunning" class="animate-pulse">评测中...</span>
                                </button>
                            </div>
                        </div>
                        <div x-show="submissionResult || submissionRunning" class="space-y-3">
                            <div class="flex items-center gap-3">
                                <span class="px-3 py-1 rounded font-mono text-[12px] font-semibold" :class="sandboxStatusClass(submissionVerdict)" x-text="submissionVerdict"></span>
                                <span class="text-[11px] text-cream-subtle" x-text="submissionRunning ? '已进入独立评测任务' : submissionStatsText()"></span>
                                <span x-show="submissionResult?.submission?.workspace_cleaned" class="text-[10px] text-success">临时工作区已清理</span>
                            </div>
                            <div x-show="submissionCompile() && submissionCompile().ok === false" class="rounded-lg bg-danger/10 border border-danger/20 p-3">
                                <p class="text-[11px] font-medium text-danger mb-2">编译失败</p>
                                <pre class="max-h-48 overflow-auto whitespace-pre-wrap text-[10px] font-mono text-cream-muted" x-text="submissionCompile()?.stderr || 'compiler failed'"></pre>
                            </div>
                            <div x-show="submissionCases().length > 0" class="rounded-lg border border-white/[0.03] overflow-hidden">
                                <div class="max-h-72 overflow-auto">
                                    <table class="w-full text-left text-[11px]">
                                        <thead class="sticky top-0 bg-ink-card"><tr class="text-cream-subtle"><th class="px-3 py-2">Case</th><th class="px-3 py-2">Verdict</th><th class="px-3 py-2">Time</th><th class="px-3 py-2">Memory</th><th class="px-3 py-2">Message</th></tr></thead>
                                        <tbody class="divide-y divide-white/[0.02]">
                                            <template x-for="item in submissionCases()" :key="item.case">
                                                <tr><td class="px-3 py-2 font-mono text-cream" x-text="item.case"></td><td class="px-3 py-2"><span class="px-2 py-0.5 rounded font-mono" :class="sandboxStatusClass(item.status)" x-text="item.status"></span></td><td class="px-3 py-2 font-mono text-cream-muted" x-text="item.time.toFixed(3) + 's'"></td><td class="px-3 py-2 font-mono text-cream-muted" x-text="item.memory == null ? '-' : item.memory.toFixed(1) + ' MiB'"></td><td class="px-3 py-2 text-cream-subtle" x-text="item.message || '-'"></td></tr>
                                            </template>
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                            <div x-show="submissionLogs" class="terminal-panel rounded-lg overflow-hidden">
                                <button @click="submissionLogOpen = !submissionLogOpen" class="w-full px-3 py-2 flex justify-between text-[10px] text-cream-subtle"><span>提交日志</span><span x-text="submissionLogOpen ? '收起' : '展开'"></span></button>
                                <pre x-show="submissionLogOpen" class="max-h-56 overflow-auto px-3 pb-3 whitespace-pre-wrap text-[10px] font-mono text-cream-muted" x-text="submissionLogs"></pre>
                            </div>
                        </div>
                    </div>

                    <div class="grid grid-cols-2 xl:grid-cols-4 gap-3" x-show="selectedIdx !== null && sandboxResult">
                        <template x-for="card in sandboxCards()" :key="card.key">
                            <div class="rounded-xl border p-4 transition-colors"
                                 :class="card.ok ? 'bg-success/10 border-success/20' : (card.warn ? 'bg-gold/10 border-gold/20' : 'bg-danger/10 border-danger/25')">
                                <div class="flex items-center justify-between mb-2">
                                    <span class="text-[11px] uppercase tracking-wide text-cream-muted" x-text="card.title"></span>
                                    <span class="text-[10px] font-mono px-2 py-0.5 rounded"
                                          :class="card.ok ? 'bg-success/15 text-success' : (card.warn ? 'bg-gold/15 text-gold' : 'bg-danger/15 text-danger')"
                                          x-text="card.status"></span>
                                </div>
                                <p class="text-[13px] text-cream" x-text="card.detail"></p>
                            </div>
                        </template>
                    </div>

                    <div x-show="selectedIdx !== null && sandboxMatrixRows().length > 0" class="rounded-xl border border-white/[0.02] overflow-hidden bg-ink-input/30">
                        <div class="px-4 py-3 border-b border-white/[0.02] flex items-center justify-between">
                            <h3 class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">用例矩阵</h3>
                            <span class="text-[10px] font-mono text-cream-subtle" x-text="sandboxMatrixPrograms().length + ' programs'"></span>
                        </div>
                        <div class="overflow-auto max-h-[360px]">
                            <table class="w-full text-left text-[12px]">
                                <thead class="sticky top-0 bg-ink-card z-10">
                                    <tr class="border-b border-white/[0.02]">
                                        <th class="px-3 py-2 font-mono text-cream-subtle">case</th>
                                        <template x-for="prog in sandboxMatrixPrograms()" :key="prog">
                                            <th class="px-3 py-2 font-mono text-cream-subtle" x-text="prog"></th>
                                        </template>
                                    </tr>
                                </thead>
                                <tbody>
                                    <template x-for="row in sandboxMatrixRows()" :key="row.case">
                                        <tr class="border-b border-white/[0.015]">
                                            <td class="px-3 py-2 font-mono text-cream-muted">
                                                <span x-text="row.case"></span>
                                                <span x-show="row.groups.length" class="block mt-1 text-[9px] text-gold" x-text="row.groups.join(', ')"></span>
                                            </td>
                                            <template x-for="prog in sandboxMatrixPrograms()" :key="prog">
                                                <td class="px-3 py-2">
                                                    <span class="inline-flex items-center gap-1.5 rounded px-2 py-0.5 font-mono text-[11px]"
                                                          :class="sandboxStatusClass(row.results[prog]?.status)"
                                                          x-text="row.results[prog] ? row.results[prog].status + ' ' + row.results[prog].time.toFixed(3) + 's' : '-'"></span>
                                                </td>
                                            </template>
                                        </tr>
                                    </template>
                                </tbody>
                            </table>
                        </div>
                    </div>

                    <div x-show="selectedIdx !== null && sandboxExpectationRows().length > 0" class="rounded-xl border border-white/[0.02] overflow-hidden bg-ink-input/30">
                        <div class="px-4 py-3 border-b border-white/[0.02] flex items-center justify-between">
                            <h3 class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">宿命断言</h3>
                            <span class="text-[10px] font-mono text-cream-subtle" x-text="sandboxExpectationRows().length + ' programs'"></span>
                        </div>
                        <div class="divide-y divide-white/[0.02]">
                            <template x-for="exp in sandboxExpectationRows()" :key="exp.program">
                                <div class="px-4 py-3 grid grid-cols-1 md:grid-cols-[minmax(0,1.4fr)_auto_minmax(0,1fr)_minmax(0,1.4fr)] gap-2 md:gap-4 items-center text-[11px]">
                                    <div>
                                        <p class="font-mono text-cream" x-text="exp.program"></p>
                                        <p class="text-cream-subtle mt-1" x-text="'status ' + (exp.expected_statuses || []).join('/') + ' · forbid ' + ((exp.forbidden_statuses || []).join('/') || '-')"></p>
                                    </div>
                                    <span class="font-mono px-2 py-0.5 rounded" :class="exp.ok ? 'bg-success/15 text-success' : 'bg-danger/15 text-danger'" x-text="exp.ok ? 'PASS' : 'FAIL'"></span>
                                    <p class="font-mono text-gold" x-text="(exp.groups && exp.groups.length) ? exp.groups.join(', ') : 'all cases'"></p>
                                    <p class="font-mono text-cream-subtle" x-text="sandboxFirstRelevant(exp) ? sandboxFirstRelevant(exp).case + ' → ' + sandboxFirstRelevant(exp).status : '-' "></p>
                                </div>
                            </template>
                        </div>
                    </div>

                    <div x-show="selectedIdx !== null" class="terminal-panel rounded-xl overflow-hidden">
                        <button @click="sandboxLogOpen = !sandboxLogOpen" class="w-full px-4 py-3 flex items-center justify-between text-left">
                            <span class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">原始日志</span>
                            <span class="text-[11px] text-cream-subtle" x-text="sandboxLogOpen ? '收起' : '展开'"></span>
                        </button>
                        <pre x-show="sandboxLogOpen" x-collapse class="max-h-[320px] overflow-auto px-4 pb-4 text-[11px] leading-relaxed font-mono text-cream-muted whitespace-pre-wrap" x-text="sandboxLogs || '暂无日志。'"></pre>
                    </div>
                </div>
            </div>

        </div>
    </div>

    <script>
        document.addEventListener('alpine:init', () => {
            Alpine.data('probhub', () => ({
                theme: document.documentElement.dataset.theme || 'dark',
                subtitles: [],
                currentSubtitle: '',
                problems: [],
                selectedIdx: null,
                activePage: 'layout',
                isCompiling: false,
                isDistributing: false,
                sandboxRunning: false,
                sandboxInfo: null,
                sandboxJobId: null,
                sandboxJobKey: '',
                sandboxResult: null,
                sandboxLogs: '',
                sandboxLastRunAt: '',
                sandboxCache: {},
                sandboxLogOpen: false,
                _sandboxPollTimer: null,
                submissionRunning: false,
                submissionJobId: null,
                submissionJobKey: '',
                submissionFilename: '',
                submissionResult: null,
                submissionLogs: '',
                submissionVerdict: 'PENDING',
                submissionLastRunAt: '',
                submissionLogOpen: false,
                submissionCache: {},
                _submissionPollTimer: null,
                pdfRefresh: Date.now(),
                pdfPages: [],
                trackWidth: 800,
                saveStatus: '',   // '' | 'saving' | 'saved' | 'error'
                tagDraft: '',
                coverConfig: { title: '', subtitle: '', author: '', date: '', logo: 'usts.png', logo_width: '9cm', logo_space_above: '0em', logo_space_below: '0em' },
                _saveTimer: null,
                _savePromise: Promise.resolve(true),
                _coverSaveTimer: null,
                toast: { show: false, msg: '', isError: false },

                toggleTheme() {
                    this.theme = this.theme === 'dark' ? 'light' : 'dark';
                    document.documentElement.dataset.theme = this.theme;
                    localStorage.setItem('probhub-theme', this.theme);
                },

                initApp() {
                    document.documentElement.dataset.theme = this.theme;
                    // 1. 初始化时拉取所有可用的排版集目录
                    fetch('/api/subtitles').then(res => res.json()).then(subs => {
                        this.subtitles = subs;
                        if (subs.length > 0) {
                            // 默认选择第一个
                            this.currentSubtitle = subs[0];
                            this.loadData();
                            this.loadConfig();
                            this.loadPdfPages();
                        }
                    });
                },

                loadData() {
                    if (!this.currentSubtitle) return;
                    // 2. 根据选中的排版集拉取对应的数据
                    fetch(`/api/data?subtitle=${encodeURIComponent(this.currentSubtitle)}`)
                        .then(res => res.json())
                        .then(data => {
                            this.problems = data;
                            this.selectedIdx = null; // 切换集子时重置选中状态
                            this.sandboxInfo = null;
                            this.sandboxResult = null;
                            this.sandboxLogs = '';
                            this.sandboxLastRunAt = '';
                            this.$nextTick(() => { this.initSortable(); });
                        });
                },

                loadPdfPages() {
                    if (!this.currentSubtitle) { this.pdfPages = []; return; }
                    const requestedSubtitle = this.currentSubtitle;
                    fetch(`/api/pdf-pages/${encodeURIComponent(requestedSubtitle)}`)
                        .then(res => res.json())
                        .then(data => {
                            if (this.currentSubtitle !== requestedSubtitle) return;
                            this.pdfPages = data.pages > 0 ? Array.from({length: data.pages}, (_, i) => i) : [];
                        });
                },

                switchSubtitle() {
                    clearTimeout(this._coverSaveTimer);
                    this.pdfPages = [];
                    this.loadData();
                    this.loadConfig();
                    this.pdfRefresh = Date.now();
                    this.loadPdfPages();
                },

                initSortable() {
                    let el = document.getElementById('sortable-list');
                    if (!el) return;
                    Sortable.create(el, {
                        animation: 200, ghostClass: 'drag-ghost',
                        onEnd: (evt) => {
                            let item = this.problems.splice(evt.oldIndex, 1)[0];
                            this.problems.splice(evt.newIndex, 0, item);
                            this.selectedIdx = evt.newIndex;
                            this.autoSave();
                        }
                    });
                },

                // ── Difficulty ──────────────────────────────────────────────
                difficultyLevels: [
                    { label: 'Very Easy',    color: '#9b7ec4', bg: 'rgba(155,126,196,0.18)' },
                    { label: 'Easy',         color: '#5a8ec0', bg: 'rgba( 90,142,192,0.18)' },
                    { label: 'Easy-Medium',  color: '#6b9b6a', bg: 'rgba(107,155,106,0.18)' },
                    { label: 'Medium',       color: '#c8a050', bg: 'rgba(200,160, 80,0.18)' },
                    { label: 'Medium-Hard',  color: '#e08840', bg: 'rgba(224,136, 64,0.18)' },
                    { label: 'Hard',         color: '#e05555', bg: 'rgba(224, 85, 85,0.18)' },
                ],

                getDifficulty(idx) {
                    let p = this.problems[idx];
                    if (!p || !p.problem) return 3; // default Medium
                    return (typeof p.problem.difficulty === 'number' && p.problem.difficulty >= 0 && p.problem.difficulty <= 5)
                        ? p.problem.difficulty : 3;
                },

                getDifficultyInfo(idx) {
                    return this.difficultyLevels[this.getDifficulty(idx)];
                },

                getTimeLimit(idx) {
                    let p = this.problems[idx];
                    let v = p && p.problem ? Number(p.problem.time_limit) : NaN;
                    return Number.isFinite(v) && v > 0 ? v : 1;
                },

                getMemoryLimit(idx) {
                    let p = this.problems[idx];
                    let v = p && p.problem ? Number(p.problem.memory_limit) : NaN;
                    return Number.isFinite(v) && v > 0 ? Math.round(v) : 256;
                },

                setTimeLimit(idx, value) {
                    let p = this.problems[idx];
                    if (!p) return;
                    if (!p.problem) p.problem = {};
                    let v = Number(value);
                    p.problem.time_limit = Number.isFinite(v) && v > 0 ? v : 1;
                    this.autoSave();
                    if (this.activePage === 'sandbox') this.refreshSandboxInfo();
                },

                setMemoryLimit(idx, value) {
                    let p = this.problems[idx];
                    if (!p) return;
                    if (!p.problem) p.problem = {};
                    let v = Number(value);
                    p.problem.memory_limit = Number.isFinite(v) && v > 0 ? Math.round(v) : 256;
                    this.autoSave();
                    if (this.activePage === 'sandbox') this.refreshSandboxInfo();
                },

                setDifficulty(idx, level) {
                    let p = this.problems[idx];
                    if (!p.problem) p.problem = {};
                    p.problem.difficulty = Math.max(0, Math.min(5, level));
                    this.autoSave();
                },

                setDifficultyFromTrack(idx, event) {
                    let el = event.currentTarget;
                    let rect = el.getBoundingClientRect();
                    let ratio = (event.clientX - rect.left) / rect.width;
                    this.setDifficulty(idx, Math.round(ratio * 5));
                },

                // ── Tags ────────────────────────────────────────────────────
                getTags(idx) {
                    let p = this.problems[idx];
                    return (p && p.problem && Array.isArray(p.problem.tags)) ? p.problem.tags : [];
                },

                startAddTag(idx) {
                    // Legacy – kept for potential reuse; new tags are added in-editor
                },

                commitTag() {
                    let tag = this.tagDraft.trim().replace(/,/g, '').trim();
                    if (!tag) { this.tagDraft = ''; return; }
                    let idx = this.selectedIdx;
                    if (idx === null) return;
                    let p = this.problems[idx];
                    if (!p.problem) p.problem = {};
                    if (!p.problem.tags) p.problem.tags = [];
                    if (!p.problem.tags.includes(tag)) {
                        p.problem.tags.push(tag);
                        this.autoSave();
                    }
                    this.tagDraft = '';
                },

                addTag(idx, tag) {
                    let p = this.problems[idx];
                    if (!p.problem) p.problem = {};
                    if (!p.problem.tags) p.problem.tags = [];
                    if (!p.problem.tags.includes(tag)) {
                        p.problem.tags.push(tag);
                        this.autoSave();
                    }
                },

                removeTag(idx, tagIdx) {
                    let p = this.problems[idx];
                    if (!p.problem || !p.problem.tags) return;
                    p.problem.tags.splice(tagIdx, 1);
                    this.autoSave();
                },

                // ── Cover config ────────────────────────────────────────────
                loadConfig() {
                    if (!this.currentSubtitle) return;
                    fetch(`/api/config/${encodeURIComponent(this.currentSubtitle)}`)
                        .then(res => res.json())
                        .then(data => { if (data.success) this.coverConfig = data.config; });
                },

                autoSaveCover() {
                    clearTimeout(this._coverSaveTimer);
                    this._coverSaveTimer = setTimeout(() => this.saveConfig(), 800);
                },

                saveConfig() {
                    if (!this.currentSubtitle) return;
                    fetch(`/api/config/${encodeURIComponent(this.currentSubtitle)}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(this.coverConfig)
                    }).then(async res => ({ status: res.status, data: await res.json() })).then(({ status, data }) => {
                        if (data.success) {
                            this.coverConfig = data.config || this.coverConfig;
                        } else if (status === 409) {
                            this.showToast('封面保存冲突：工作区已被其他会话修改，请刷新后重试。', true, 8000);
                        }
                    });
                },

                // ── Dashboard stats ─────────────────────────────────────────
                getDifficultyStats() {
                    const counts = [0, 0, 0, 0, 0, 0];
                    this.problems.forEach((p, i) => { counts[this.getDifficulty(i)]++; });
                    return counts;
                },

                getAllTags() {
                    const tags = new Set();
                    this.problems.forEach(p => {
                        if (p.problem && Array.isArray(p.problem.tags)) {
                            p.problem.tags.forEach(t => tags.add(t));
                        }
                    });
                    return [...tags].sort();
                },

                // ── Sandbox ────────────────────────────────────────────────
                sandboxKey(index = this.selectedIdx) {
                    if (!this.currentSubtitle || index === null || index === undefined) return '';
                    return `${this.currentSubtitle}::${index}`;
                },

                restoreSandboxCache() {
                    const cached = this.sandboxCache[this.sandboxKey()];
                    if (cached) {
                        this.sandboxResult = cached.result || null;
                        this.sandboxLogs = cached.logs || '';
                        this.sandboxLastRunAt = cached.finishedAt || '';
                    } else {
                        this.sandboxResult = null;
                        this.sandboxLogs = '';
                        this.sandboxLastRunAt = '';
                    }
                },

                refreshSandboxInfo() {
                    this.restoreSandboxCache();
                    this.restoreSubmissionCache();
                    if (!this.currentSubtitle || this.selectedIdx === null) {
                        this.sandboxInfo = null;
                        return;
                    }
                    fetch(`/api/sandbox/problem?subtitle=${encodeURIComponent(this.currentSubtitle)}&index=${this.selectedIdx}`)
                        .then(res => res.json())
                        .then(data => {
                            this.sandboxInfo = data.success ? data.info : { matched: false, reason: data.error || 'load failed' };
                        })
                        .catch(() => {
                            this.sandboxInfo = { matched: false, reason: 'network error' };
                        });
                },

                async runSandbox() {
                    if (!this.currentSubtitle || this.selectedIdx === null) return;
                    clearTimeout(this._saveTimer);
                    await this._doSave();
                    const jobKey = this.sandboxKey();
                    this.sandboxRunning = true;
                    this.sandboxLogOpen = true;
                    fetch('/api/sandbox/run', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ subtitle: this.currentSubtitle, index: this.selectedIdx })
                    }).then(res => res.json()).then(data => {
                        if (!data.success) {
                            this.sandboxRunning = false;
                            this.showToast(data.error || 'Sandbox failed to start', true);
                            return;
                        }
                        this.sandboxJobId = data.job_id;
                        this.sandboxJobKey = jobKey;
                        this.pollSandboxJob(data.job_id, jobKey);
                    }).catch(() => {
                        this.sandboxRunning = false;
                        this.showToast('Sandbox failed to start', true);
                    });
                },

                pollSandboxJob(jobId = this.sandboxJobId, jobKey = this.sandboxJobKey) {
                    if (!jobId || !jobKey) return;
                    fetch(`/api/sandbox/job/${jobId}`)
                        .then(res => res.json())
                        .then(data => {
                            if (!data.success) throw new Error(data.error || 'job missing');
                            const cacheEntry = this.sandboxCache[jobKey] || {};
                            cacheEntry.logs = data.logs || '';
                            cacheEntry.result = data.result || null;
                            this.sandboxCache[jobKey] = cacheEntry;
                            if (this.sandboxKey() === jobKey) {
                                this.sandboxLogs = cacheEntry.logs;
                                this.sandboxResult = cacheEntry.result;
                                this.sandboxLastRunAt = cacheEntry.finishedAt || '';
                            }
                            if (data.status === 'running') {
                                this._sandboxPollTimer = setTimeout(() => this.pollSandboxJob(jobId, jobKey), 900);
                            } else {
                                const finishedAt = new Date().toLocaleTimeString('zh-CN', { hour12: false });
                                this.sandboxCache[jobKey] = {
                                    result: data.result || null,
                                    logs: data.logs || '',
                                    finishedAt,
                                };
                                if (this.sandboxKey() === jobKey) {
                                    this.sandboxResult = this.sandboxCache[jobKey].result;
                                    this.sandboxLogs = this.sandboxCache[jobKey].logs;
                                    this.sandboxLastRunAt = finishedAt;
                                }
                                if (this.sandboxJobId === jobId) this.sandboxRunning = false;
                                if (data.status === 'success') this.showToast('Sandbox finished');
                                else this.showToast('Sandbox found issues', true);
                                this.refreshSandboxInfo();
                            }
                        })
                        .catch(() => {
                            this.sandboxRunning = false;
                            this.showToast('Sandbox job lost', true);
                        });
                },

                sandboxCards() {
                    const r = this.sandboxResult || {};
                    const summaries = r.summaries || {};
                    const compile = r.compiles || [];
                    const validatorEvents = r.validator || [];
                    const compileFailed = (kind) => compile.some(c => c.kind === kind && c.ok === false);
                    const compileSkipped = (kind) => compile.some(c => c.kind === kind && c.ok === null);
                    const compileItem = (kind) => compile.find(c => c.kind === kind);
                    const sumFor = (kind) => Object.values(summaries).filter(s => s.kind === kind);
                    const detailFor = (items) => items.length
                        ? items.map(s => {
                            const exp = s.expectation || {};
                            const fate = Object.keys(exp).length ? ` · fate ${exp.ok ? 'PASS' : 'FAIL'}` : '';
                            return `${s.program}: AC ${s.stats.AC || 0}, WA ${s.stats.WA || 0}, TLE ${s.stats.TLE || 0}, MLE ${s.stats.MLE || 0}, RE ${s.stats.RE || 0}${fate}`;
                        }).join(' · ')
                        : 'No run';
                    const expectationOk = (items) => items.length > 0 && items.every(s => (s.expectation || {}).ok === true);
                    const std = sumFor('std');
                    const brute = sumFor('brute');
                    const wrong = sumFor('wrong');
                    const validatorOk = validatorEvents.length > 0 && validatorEvents.every(v => v.ok);
                    return [
                        {
                            key: 'validator', title: 'Validator',
                            ok: validatorOk || compileSkipped('validator'), warn: compileSkipped('validator'),
                            status: compileFailed('validator') ? 'CE' : (compileSkipped('validator') ? 'SKIP' : (validatorOk ? 'PASS' : 'FAIL')),
                            detail: compileSkipped('validator') ? `${compileItem('validator')?.file || 'validator'} not found` : `${validatorEvents.filter(v => v.ok).length}/${validatorEvents.length} cases valid`
                        },
                        {
                            key: 'std', title: 'Standard',
                            ok: expectationOk(std),
                            warn: false,
                            status: compileFailed('std') ? 'CE' : (std.length ? 'DONE' : 'FAIL'),
                            detail: detailFor(std)
                        },
                        {
                            key: 'brute', title: 'Brute',
                            ok: expectationOk(brute),
                            warn: brute.length === 0,
                            status: brute.length ? 'DONE' : 'SKIP',
                            detail: detailFor(brute)
                        },
                        {
                            key: 'wrong', title: 'Wrong',
                            ok: expectationOk(wrong),
                            warn: wrong.length === 0,
                            status: wrong.length ? 'DONE' : 'SKIP',
                            detail: detailFor(wrong)
                        },
                    ];
                },

                sandboxMatrixPrograms() {
                    const cases = (this.sandboxResult && this.sandboxResult.cases) || [];
                    return [...new Set(cases.map(c => c.program))];
                },

                sandboxMatrixRows() {
                    const cases = (this.sandboxResult && this.sandboxResult.cases) || [];
                    const rows = {};
                    cases.forEach(c => {
                        if (!rows[c.case]) rows[c.case] = { case: c.case, groups: c.groups || [], results: {} };
                        rows[c.case].groups = [...new Set([...(rows[c.case].groups || []), ...(c.groups || [])])];
                        rows[c.case].results[c.program] = c;
                    });
                    return Object.values(rows).sort((a, b) => a.case.localeCompare(b.case, undefined, { numeric: true }));
                },

                sandboxExpectationRows() {
                    const expectations = (this.sandboxResult && this.sandboxResult.expectations) || {};
                    return Object.values(expectations).sort((a, b) => String(a.program).localeCompare(String(b.program)));
                },

                sandboxFirstRelevant(expectation) {
                    return expectation.first_forbidden || expectation.first_expected_match || expectation.first_non_ac || null;
                },

                handleSubmissionFile(event) {
                    const file = event.target.files && event.target.files[0];
                    this.submissionFilename = file ? file.name : '';
                },

                restoreSubmissionCache() {
                    const cached = this.submissionCache[this.sandboxKey()];
                    this.submissionResult = cached?.result || null;
                    this.submissionLogs = cached?.logs || '';
                    this.submissionVerdict = cached?.verdict || 'PENDING';
                    this.submissionLastRunAt = cached?.finishedAt || '';
                },

                runSubmission() {
                    const file = this.$refs.submissionFile?.files?.[0];
                    if (!file || !this.currentSubtitle || this.selectedIdx === null) return;
                    const form = new FormData();
                    form.append('subtitle', this.currentSubtitle);
                    form.append('index', String(this.selectedIdx));
                    form.append('source', file, file.name);
                    const jobKey = this.sandboxKey();
                    this.submissionRunning = true;
                    this.submissionResult = null;
                    this.submissionLogs = '';
                    this.submissionVerdict = 'PENDING';
                    this.submissionLogOpen = true;
                    fetch('/api/submission/run', { method: 'POST', body: form })
                        .then(async res => ({ ok: res.ok, data: await res.json() }))
                        .then(({ ok, data }) => {
                            if (!ok || !data.success) throw new Error(data.error || 'submission failed to start');
                            this.submissionJobId = data.job_id;
                            this.submissionJobKey = jobKey;
                            this.submissionCache[jobKey] = { result: null, logs: '', verdict: 'PENDING', finishedAt: '' };
                            this.pollSubmissionJob(data.job_id, jobKey);
                        })
                        .catch(error => {
                            this.submissionRunning = false;
                            this.submissionVerdict = 'FAIL';
                            this.showToast(error.message || '提交失败', true, 6000);
                        });
                },

                cancelSubmission() {
                    if (!this.submissionJobId || !this.submissionRunning) return;
                    this.submissionVerdict = 'CANCELLING';
                    fetch(`/api/submission/job/${this.submissionJobId}/cancel`, { method: 'POST' })
                        .then(res => res.json())
                        .then(data => {
                            if (!data.success) throw new Error(data.error || 'cancel failed');
                        })
                        .catch(error => this.showToast(error.message || '取消失败', true, 6000));
                },

                pollSubmissionJob(jobId = this.submissionJobId, jobKey = this.submissionJobKey) {
                    if (!jobId || !jobKey) return;
                    fetch(`/api/submission/job/${jobId}`)
                        .then(res => res.json())
                        .then(data => {
                            if (!data.success) throw new Error(data.error || 'submission job missing');
                            const verdict = data.status === 'cancelling' ? 'CANCELLING' : (data.verdict || data.result?.submission?.verdict || 'PENDING');
                            const cacheEntry = {
                                result: data.result || null,
                                logs: data.logs || '',
                                verdict,
                                finishedAt: this.submissionCache[jobKey]?.finishedAt || '',
                            };
                            this.submissionCache[jobKey] = cacheEntry;
                            if (this.sandboxKey() === jobKey) {
                                this.submissionLogs = cacheEntry.logs;
                                this.submissionResult = cacheEntry.result;
                                this.submissionVerdict = verdict;
                            }
                            if (data.status === 'queued' || data.status === 'running' || data.status === 'cancelling') {
                                this._submissionPollTimer = setTimeout(() => this.pollSubmissionJob(jobId, jobKey), 700);
                                return;
                            }
                            this.submissionRunning = false;
                            const finishedAt = new Date().toLocaleTimeString('zh-CN', { hour12: false });
                            this.submissionCache[jobKey].finishedAt = finishedAt;
                            if (this.sandboxKey() === jobKey) this.submissionLastRunAt = finishedAt;
                            if (data.status === 'completed') this.showToast(`提交评测完成：${verdict}`, verdict !== 'AC');
                            else if (data.status === 'cancelled') this.showToast('提交评测已取消');
                            else this.showToast('提交评测基础设施失败', true, 6000);
                        })
                        .catch(error => {
                            this.submissionRunning = false;
                            this.submissionVerdict = 'FAIL';
                            this.showToast(error.message || '提交任务丢失', true, 6000);
                        });
                },

                submissionCompile() {
                    const items = (this.submissionResult && this.submissionResult.compiles) || [];
                    return [...items].reverse().find(item => item.kind === 'std') || null;
                },

                submissionCases() {
                    return ((this.submissionResult && this.submissionResult.cases) || []).filter(item => item.kind === 'std');
                },

                submissionStatsText() {
                    const counts = {};
                    this.submissionCases().forEach(item => { counts[item.status] = (counts[item.status] || 0) + 1; });
                    const detail = ['AC', 'WA', 'TLE', 'MLE', 'OLE', 'RE', 'FAIL'].filter(key => counts[key]).map(key => `${key} ${counts[key]}`).join(' · ');
                    return detail || (this.submissionCompile()?.ok === false ? '编译失败' : '暂无测试点结果');
                },

                sandboxStatusClass(status) {
                    if (status === 'AC') return 'bg-success/15 text-success';
                    if (status === 'WA') return 'bg-danger/15 text-danger';
                    if (status === 'TLE') return 'bg-gold/15 text-gold';
                    if (status === 'MLE') return 'bg-danger/15 text-danger';
                    if (status === 'OLE') return 'bg-gold/15 text-gold';
                    if (status === 'CANCELLING' || status === 'CANCELLED') return 'bg-ink-elevated text-cream-subtle';
                    if (status === 'CE' || status === 'RE' || status === 'FAIL') return 'bg-danger/20 text-danger';
                    return 'bg-ink-elevated text-cream-subtle';
                },

                selectProb(index) {
                    this.selectedIdx = index;
                    this.tagDraft = '';
                    if (this.$refs.submissionFile) this.$refs.submissionFile.value = '';
                    this.submissionFilename = '';
                    this.restoreSubmissionCache();
                    if (this.activePage === 'sandbox') this.refreshSandboxInfo();
                },
                
                hasQuote() {
                    if (this.selectedIdx === null || !this.problems[this.selectedIdx]) return false;
                    return this.problems[this.selectedIdx].statement && this.problems[this.selectedIdx].statement.quote !== undefined;
                },

                toggleQuote(enable) {
                    let p = this.problems[this.selectedIdx];
                    if (!p.statement) p.statement = {};
                    if (enable) {
                        p.statement.quote = { text: "", source: "" };
                    } else {
                        delete p.statement.quote;
                    }
                    this.autoSave();
                },

                addSample() {
                    let p = this.problems[this.selectedIdx];
                    if (!p.problem) return;
                    if (!p.problem.samples) p.problem.samples = [];
                    p.problem.samples.push({ input: "", output: "" });
                    this.autoSave();
                },

                removeSample(index) {
                    let p = this.problems[this.selectedIdx];
                    if (!p.problem || !p.problem.samples) return;
                    if (p.problem.samples.length <= 1) return;
                    p.problem.samples.splice(index, 1);
                    this.autoSave();
                },

                renderMath(el, text) {
                    if (!el) return;
                    if (!text) {
                        el.innerHTML = '<span class="text-cream-subtle italic text-[12px]">暂无内容...</span>';
                        return;
                    }
                    el.innerHTML = marked.parse(text);
                    if (window.MathJax && window.MathJax.typesetPromise) {
                        MathJax.typesetClear([el]);
                        MathJax.typesetPromise([el]).catch(err => console.error('MathJax:', err));
                    }
                },

                // ── Auto-save ───────────────────────────────────────────────
                autoSave() {
                    clearTimeout(this._saveTimer);
                    this.saveStatus = 'saving';
                    this._saveTimer = setTimeout(() => this._queueSave(), 800);
                },

                _mergeSavedMetadata(savedProblems) {
                    const savedById = new Map((savedProblems || []).map(problem => [problem._id, problem]));
                    this.problems.forEach(problem => {
                        const saved = savedById.get(problem._id);
                        if (!saved) return;
                        problem._revision = saved._revision;
                        problem._workspace_revision = saved._workspace_revision;
                        const liveSamples = (problem.problem && problem.problem.samples) || [];
                        const savedSamples = (saved.problem && saved.problem.samples) || [];
                        liveSamples.forEach((sample, index) => {
                            if (savedSamples[index] && savedSamples[index]._name) {
                                sample._name = savedSamples[index]._name;
                            }
                        });
                    });
                },

                _queueSave() {
                    this._savePromise = this._savePromise.then(() => this._performSave());
                    return this._savePromise;
                },

                _performSave() {
                    if (!this.currentSubtitle) return Promise.resolve(false);
                    const subtitle = this.currentSubtitle;
                    const payload = JSON.parse(JSON.stringify(this.problems));
                    this.saveStatus = 'saving';
                    return fetch('/api/data', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ subtitle, problems: payload })
                    }).then(async res => ({ ok: res.ok, status: res.status, data: await res.json() })).then(({ status, data }) => {
                        if (data.success) {
                            if (subtitle !== this.currentSubtitle) return true;
                            if (Array.isArray(data.problems)) this._mergeSavedMetadata(data.problems);
                            this.saveStatus = 'saved';
                            setTimeout(() => { if (this.saveStatus === 'saved') this.saveStatus = ''; }, 2500);
                            return true;
                        } else {
                            this.saveStatus = 'error';
                            const message = status === 409
                                ? '保存冲突：题目已被其他会话修改，请刷新后重试。'
                                : (data.error || '保存失败');
                            this.showToast(message, true, 8000);
                            return false;
                        }
                    }).catch(() => { this.saveStatus = 'error'; return false; });
                },

                doSave() {
                    clearTimeout(this._saveTimer);
                    return this._queueSave();
                },

                async compilePDF() {
                    if (!this.currentSubtitle) return;
                    clearTimeout(this._saveTimer);
                    if (!(await this.doSave())) return;
                    this.isCompiling = true;
                    fetch('/api/compile', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ subtitle: this.currentSubtitle })
                    }).then(res => res.json()).then(data => {
                        this.isCompiling = false;
                        if (data.success) {
                            this.pdfRefresh = Date.now();
                            this.loadPdfPages();
                            this.showToast(`[${this.currentSubtitle}] Typst compile OK`);
                        } else {
                            const message = data.message || data.error || 'Typst 编译失败';
                            const suggestion = data.suggestion ? `建议：${data.suggestion}` : '建议：查看终端中的 Typst 报错定位具体语法位置。';
                            this.showToast(`${message}。${suggestion}`, true, 8000);
                        }
                    }).catch(() => {
                        this.isCompiling = false;
                        this.showToast('编译请求失败。建议：确认 ui.py 服务仍在运行，并检查终端日志。', true, 8000);
                    });
                },
                async distributePDFs() {
                    if (!this.currentSubtitle) return;
                    clearTimeout(this._saveTimer);
                    if (!(await this.doSave())) return;
                    this.isDistributing = true;
                    fetch('/api/distribute', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ subtitle: this.currentSubtitle })
                    }).then(res => res.json()).then(data => {
                        this.isDistributing = false;
                        if (data.success) {
                            let msg = `[${this.currentSubtitle}] PDF distribution done`;
                            if (data.distributed && data.distributed.length > 0) {
                                let ok = data.distributed.filter(d => d.status === 'ok').length;
                                let zipUpdated = data.distributed.filter(d => d.zip === 'updated').length;
                                msg += ` — ${ok}/${data.distributed.length} PDFs extracted`;
                                if (zipUpdated > 0) msg += `, ${zipUpdated} zip(s) updated`;
                            }
                            this.showToast(msg);
                        } else {
                            this.showToast('Distribution failed', true);
                        }
                    });
                },

                showToast(msg, isError = false, duration = 3000) {
                    this.toast = { show: true, msg, isError };
                    setTimeout(() => { this.toast.show = false; }, duration);
                }
            }));
        });
    </script>
</body>
</html>
"""

# ==========================================
# 后端 API 路由
# ==========================================

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/subtitles', methods=['GET'])
def get_subtitles():
    """动态扫描 typst-statement 目录下所有的子文件夹（即排版集）"""
    if not os.path.exists(BASE_DIR):
        return jsonify([])
    # 仅返回是目录的名称
    subs = [d for d in os.listdir(BASE_DIR) if os.path.isdir(os.path.join(BASE_DIR, d))]
    return jsonify(sorted(subs))

@app.route('/api/data', methods=['GET'])
def get_data():
    subtitle = request.args.get('subtitle')
    if not subtitle:
        return jsonify([])
    try:
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, workspace = schema
            return jsonify(load_editor_data(root, workspace))
        json_path = secure_path(subtitle, "problems.json")
        if not os.path.exists(json_path):
            return jsonify([])
        with open(json_path, 'r', encoding='utf-8') as f:
            return jsonify(enrich_problem_limits(json.load(f)))
    except Exception as e:
        print(f"[-] Data Load Error: {e}")
        return jsonify([])

@app.route('/api/data', methods=['POST'])
def save_data():
    payload = request.json
    subtitle = payload.get('subtitle')
    new_data = payload.get('problems', [])
    
    if not subtitle:
        return jsonify({"success": False, "error": "Missing subtitle"})
        
    try:
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, _ = schema
            with workspace_build_lock(root):
                _, live_workspace = load_workspace(root)
                problems = save_editor_data(root, live_workspace, new_data)
            return jsonify({"success": True, "problems": problems})
        json_path = secure_path(subtitle, "problems.json")
        for entry in new_data:
            entry.setdefault("problem", {})
            time_limit, memory_limit = problem_limits(entry)
            entry["problem"]["time_limit"] = time_limit
            entry["problem"]["memory_limit"] = memory_limit
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        tmp_path = json_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, json_path)  # atomic replace
        sync_problem_limits_to_files(new_data)
        return jsonify({"success": True})
    except ProbHubError as e:
        status = 409 if e.code == "source_conflict" else 400
        return jsonify({"success": False, "error": str(e), "code": e.code}), status
    except Exception as e:
        print(f"[-] Save Data Error: {e}")
        return jsonify({"success": False, "error": str(e)})

def analyze_compile_error(output):
    text = (output or "").strip()
    lower = text.lower()
    if not text:
        return {
            "message": "Typst 编译失败，但没有返回详细错误",
            "suggestion": "查看启动 ui.py 的终端，确认 typst 是否正常安装并可执行。",
        }
    if any(marker in lower for marker in (
        "being used by another process",
        "os error 32",
        "access is denied",
        "permission denied",
        "cannot access the file",
        "failed to create file",
        "failed to write",
    )):
        return {
            "message": "PDF 输出文件可能正被占用，Typst 无法覆盖写入",
            "suggestion": "先关闭正在打开 main.pdf 的 PDF 查看器或浏览器预览页，再重新编译。",
        }
    if "not found" in lower and "typst" in lower:
        return {
            "message": "未找到 typst 命令",
            "suggestion": "确认 Typst 已安装，并且 typst 命令已加入 PATH。",
        }
    if any(marker in lower for marker in ("failed to load", "file not found", "no such file")):
        return {
            "message": "Typst 编译时找不到某个引用文件",
            "suggestion": "检查图片、PDF、Typst include/import 路径是否存在，尤其是题面中的相对路径资源。",
        }
    if "error:" in lower or "expected" in lower or "unknown" in lower:
        first_error = next((line.strip() for line in text.splitlines() if line.strip()), "Typst 语法错误")
        return {
            "message": first_error[:160],
            "suggestion": "根据 Typst 报错中的文件名和行列号检查刚修改的题面、封面设置或 quote 内容。",
        }
    return {
        "message": text.splitlines()[0][:160],
        "suggestion": "查看启动 ui.py 的终端输出，定位 Typst 返回的完整错误信息。",
    }

@app.route('/api/compile', methods=['POST'])
def compile_pdf():
    """仅编译 typst，不分发 PDF。"""
    payload = request.json
    subtitle = payload.get('subtitle')
    if not subtitle:
        return jsonify({"success": False, "error": "Missing subtitle"})
    try:
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, _ = schema
            with workspace_build_lock(root):
                _, live_workspace = load_workspace(root)
                entries = problem_entries(live_workspace)
                plan = create_build_plan(root, live_workspace, entries)
                with create_build_snapshot(plan) as snapshot:
                    _, staged_pdf, _ = compile_collection(
                        snapshot.root,
                        snapshot.workspace,
                        snapshot.loaded_problems,
                    )
                    preview_pdf = _preview_pdf_path(subtitle)
                    preview_pdf.parent.mkdir(parents=True, exist_ok=True)
                    temporary = preview_pdf.with_suffix(".pdf.tmp")
                    shutil.copyfile(staged_pdf, temporary)
                    os.replace(temporary, preview_pdf)
            return jsonify({"success": True, "preview": True})
        typst_main = secure_path(subtitle, "main.typ")
        ret = subprocess.run(
            ["typst", "compile", "--root", ".", typst_main],
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
        )
        return jsonify({"success": True, "stdout": ret.stdout, "stderr": ret.stderr})
    except ProbHubError as e:
        detail = analyze_compile_error(str(e))
        status = 409 if e.code in {"build_busy", "inputs_changed"} else 400
        return jsonify({"success": False, "error": str(e), "code": e.code, **detail}), status
    except FileNotFoundError as e:
        detail = analyze_compile_error("typst not found")
        return jsonify({"success": False, "error": str(e), **detail})
    except subprocess.CalledProcessError as e:
        output = "\n".join(part for part in (e.stderr, e.stdout, str(e)) if part)
        detail = analyze_compile_error(output)
        print(f"[-] Typst compile failed:\n{output}")
        return jsonify({"success": False, "error": str(e), "output": output[-4000:], **detail})

@app.route('/api/distribute', methods=['POST'])
def distribute_pdfs():
    """仅分发单题 PDF + 注入同名 zip，不编译。"""
    payload = request.json
    subtitle = payload.get('subtitle')
    if not subtitle:
        return jsonify({"success": False, "error": "Missing subtitle"})
    try:
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, workspace = schema
            result = build_workspace(root, workspace, problem_entries(workspace))
            distributed = [
                {
                    "name": problem_id,
                    "status": "ok",
                    "dir": str(Path(details["path"]).parent),
                    "zip": "verified",
                }
                for problem_id, details in result["pdfs"].items()
            ]
            return jsonify({
                "success": True,
                "distributed": distributed,
                "batch_id": result["batch_id"],
            })
        dist_results = distribute_problems(subtitle)
        return jsonify({"success": True, "distributed": dist_results})
    except ProbHubError as e:
        status = 409 if e.code in {"build_busy", "inputs_changed"} else 400
        return jsonify({"success": False, "error": str(e), "code": e.code}), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


def find_problem_dirs():
    """Scan workspace root for problem directories (must contain meta.json and data/)."""
    problem_dirs = {}
    for entry in os.listdir("."):
        if not os.path.isdir(entry):
            continue
        meta = os.path.join(entry, "meta.json")
        data_dir = os.path.join(entry, "data")
        if os.path.isfile(meta) and os.path.isdir(data_dir):
            try:
                with open(meta, "r", encoding="utf-8") as f:
                    info = json.load(f)
                name = info.get("problem", {}).get("display_name", "")
                if name:
                    problem_dirs[name] = entry
            except Exception:
                pass
    return problem_dirs


def _zip_candidate_names(base, filename):
    return {filename, f"{base}/{filename}"}


def _zip_preferred_arcname(infos, base, filename):
    names = {item.filename for item in infos}
    if filename in names:
        return filename
    based = f"{base}/{filename}"
    if based in names:
        return based
    root_style_markers = {"problem.yaml", "domjudge-problem.ini", "problem.pdf", "data/"}
    if any(name in root_style_markers or name.startswith("data/") for name in names):
        return filename
    if any(name.startswith(f"{base}/") for name in names):
        return based
    return filename


def sync_problem_limits_to_files(problem_entries):
    """Best-effort sync of per-problem limits to meta.json and DOMjudge config files."""
    name_to_dir = find_problem_dirs()
    for entry in problem_entries:
        display_name = entry.get("problem", {}).get("display_name", "")
        prob_dir = name_to_dir.get(display_name)
        if not prob_dir:
            continue
        time_limit, memory_limit = problem_limits(entry)

        meta_path = os.path.join(prob_dir, "meta.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                meta.setdefault("problem", {})
                meta["problem"]["time_limit"] = time_limit
                meta["problem"]["memory_limit"] = memory_limit
                tmp = meta_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                os.replace(tmp, meta_path)
            except Exception as e:
                print(f"[-] Limit sync meta failed for {prob_dir}: {e}")

        ini_path = os.path.join(prob_dir, "domjudge-problem.ini")
        try:
            if os.path.exists(ini_path):
                with open(ini_path, "r", encoding="utf-8") as f:
                    text = f.read()
                if re.search(r"(?m)^timelimit\s*=", text):
                    text = re.sub(r"(?m)^timelimit\s*=.*$", f"timelimit='{time_limit:g}'", text)
                else:
                    text = (text.rstrip() + f"\ntimelimit='{time_limit:g}'\n").lstrip()
            else:
                text = f"timelimit='{time_limit:g}'\n"
            with open(ini_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"[-] Limit sync ini failed for {prob_dir}: {e}")

        yaml_path = os.path.join(prob_dir, "problem.yaml")
        try:
            if os.path.exists(yaml_path):
                with open(yaml_path, "r", encoding="utf-8") as f:
                    text = f.read()
                if re.search(r"(?m)^\s*memory\s*:", text):
                    text = re.sub(r"(?m)^(\s*)memory\s*:.*$", rf"\1memory: {memory_limit}", text)
                elif re.search(r"(?m)^limits\s*:", text):
                    text = re.sub(r"(?m)^limits\s*:\s*$", f"limits:\n  memory: {memory_limit}", text)
                else:
                    text = text.rstrip() + f"\nlimits:\n  memory: {memory_limit}\n"
            else:
                safe_name = str(display_name).replace("'", "''")
                text = f"name: '{safe_name}'\nlimits:\n  memory: {memory_limit}\n"
            with open(yaml_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"[-] Limit sync yaml failed for {prob_dir}: {e}")

        zip_path = os.path.join(os.path.basename(os.path.normpath(prob_dir)) + ".zip")
        if os.path.exists(zip_path):
            try:
                import zipfile as zf
                base = os.path.basename(os.path.normpath(prob_dir))
                replacements = {}
                for filename in ("domjudge-problem.ini", "problem.yaml"):
                    path = os.path.join(prob_dir, filename)
                    if os.path.exists(path):
                        with open(path, "rb") as f:
                            replacements[filename] = f.read()
                if replacements:
                    tmp = zip_path + ".tmp"
                    with zf.ZipFile(zip_path, "r") as zin, zf.ZipFile(tmp, "w", compression=zf.ZIP_DEFLATED) as zout:
                        infos = zin.infolist()
                        arc_replacements = {
                            _zip_preferred_arcname(infos, base, filename): content
                            for filename, content in replacements.items()
                        }
                        skip_names = set()
                        for filename in replacements:
                            skip_names.update(_zip_candidate_names(base, filename))
                        for item in zin.infolist():
                            if item.filename not in skip_names:
                                zout.writestr(item, zin.read(item.filename))
                        for arcname, content in arc_replacements.items():
                            zout.writestr(arcname, content)
                    os.replace(tmp, zip_path)
            except Exception as e:
                print(f"[-] Limit sync zip failed for {prob_dir}: {e}")


def _load_problem_by_index(subtitle, index):
    schema = _schema_workspace(subtitle)
    if schema is not None:
        root, workspace = schema
        entries = problem_entries(workspace)
        if index < 0 or index >= len(entries):
            raise ValueError("Problem index out of range")
        data = load_editor_data(root, workspace)
        return data[index]
    json_path = secure_path(subtitle, "problems.json")
    with open(json_path, "r", encoding="utf-8") as f:
        problems = json.load(f)
    if index < 0 or index >= len(problems):
        raise ValueError("Problem index out of range")
    return problems[index]


def _sandbox_problem_info(subtitle, index):
    problem = _load_problem_by_index(subtitle, index)
    display_name = problem.get("problem", {}).get("display_name", "")
    schema = _schema_workspace(subtitle)
    if schema is not None:
        root, workspace = schema
        entries = problem_entries(workspace)
        prob_dir = str(root / entries[index].get("directory", entries[index]["id"]))
    else:
        name_to_dir = find_problem_dirs()
        prob_dir = name_to_dir.get(display_name)
    script_path = os.path.join("scripts", "local_judge.py")
    info = {
        "name": display_name,
        "matched": bool(prob_dir),
        "dir": prob_dir,
        "runnable": False,
        "limits": {"time": problem_limits(problem)[0], "memory": problem_limits(problem)[1]},
        "data_count": 0,
        "sample_count": 0,
        "secret_count": 0,
        "cases": [],
        "files": {"validator": False, "std": [], "brute": [], "wrong": [], "other": []},
        "script_exists": os.path.exists(script_path),
    }
    if not prob_dir:
        info["reason"] = "no matching directory"
        return info

    for suite in ("sample", "secret"):
        data_dir = os.path.join(prob_dir, "data", suite)
        if os.path.isdir(data_dir):
            suite_cases = [f"{suite}/{f[:-3]}" for f in os.listdir(data_dir) if f.endswith(".in")]
            info["cases"].extend(sorted(suite_cases))
            info[f"{suite}_count"] = len(suite_cases)
    info["data_count"] = len(info["cases"])

    config = read_probhub_config_from_dir(prob_dir)
    if config is not None:
        judge = config.get("judge") or {}
        validator = _normalized_config_path(judge.get("validator"))
        if validator and os.path.isfile(os.path.join(prob_dir, validator)):
            info["files"]["validator"] = validator

        solutions = config.get("solutions") or {}
        for config_key, display_key in (("accepted", "std"), ("brute", "brute"), ("wrong", "wrong")):
            for entry in _config_entries(solutions.get(config_key)):
                source = _normalized_config_path(entry)
                if source and os.path.isfile(os.path.join(prob_dir, source)):
                    info["files"][display_key].append(source)

        configured = set(sum((info["files"][key] for key in ("std", "brute", "wrong")), []))
        if info["files"]["validator"]:
            configured.add(info["files"]["validator"])
        for entry in _config_entries(config.get("generators")):
            source = _normalized_config_path(entry)
            if source and source not in configured and os.path.isfile(os.path.join(prob_dir, source)):
                info["files"]["other"].append(source)
    else:
        for file in sorted(os.listdir(prob_dir)):
            if not file.endswith(".cpp"):
                continue
            if file == "validator.cpp":
                info["files"]["validator"] = file
            elif file.startswith("std"):
                info["files"]["std"].append(file)
            elif file.startswith("brute"):
                info["files"]["brute"].append(file)
            elif file.startswith("wrong"):
                info["files"]["wrong"].append(file)
            else:
                info["files"]["other"].append(file)

    info["runnable"] = info["script_exists"] and info["data_count"] > 0
    return info


def _empty_sandbox_result(info):
    return {
        "problem": info,
        "limits": info.get("limits", {"time": 1, "memory": 256}),
        "compiles": [],
        "validator": [],
        "cases": [],
        "groups": [],
        "expectations": {},
        "summaries": {},
        "cache": {},
        "transcripts": [],
        "final": None,
    }


def _apply_sandbox_event(result, event):
    typ = event.get("type")
    if typ == "limits":
        result["limits"] = {
            "time": event.get("time_limit", 1),
            "memory": event.get("memory_limit", 256),
            "output": event.get("output_limit", 64),
            "processes": event.get("process_limit", 32),
            "judge_type": event.get("judge_type", "standard"),
            "idle_limit": event.get("idle_limit"),
            "transcript_limit": event.get("transcript_limit"),
        }
    elif typ == "groups":
        result["groups"] = event.get("groups") or []
    elif typ == "compile":
        result["compiles"].append({
            "kind": event.get("kind"),
            "file": event.get("file"),
            "ok": event.get("ok"),
            "stderr": event.get("stderr", ""),
            "cached": bool(event.get("cached")),
        })
    elif typ == "validator":
        result["validator"].append({
            "case": event.get("case"),
            "ok": bool(event.get("ok")),
            "cached": bool(event.get("cached")),
        })
    elif typ == "case":
        result["cases"].append({
            "kind": event.get("kind"),
            "program": event.get("program"),
            "case": event.get("case"),
            "groups": event.get("groups") or [],
            "status": event.get("status"),
            "time": float(event.get("time") or 0),
            "memory": event.get("memory"),
            "time_limit": event.get("time_limit"),
            "memory_limit": event.get("memory_limit"),
            "memory_enforced": event.get("memory_enforced"),
            "judge_type": event.get("judge_type", "standard"),
            "message": event.get("message", ""),
            "timeout_kind": event.get("timeout_kind"),
            "transcript_truncated": bool(event.get("transcript_truncated")),
            "cached": bool(event.get("cached")),
        })
    elif typ == "transcript":
        result["transcripts"].append({
            "kind": event.get("kind"),
            "program": event.get("program"),
            "case": event.get("case"),
            "entries": event.get("entries") or [],
            "truncated": bool(event.get("truncated")),
            "cached": bool(event.get("cached")),
        })
    elif typ == "summary":
        program = event.get("program") or f"{event.get('kind', 'unknown')}-summary"
        result["summaries"][program] = {
            "kind": event.get("kind"),
            "program": program,
            "stats": event.get("stats", {}),
            "expectation": event.get("expectation") or {},
        }
    elif typ == "expectation":
        program = event.get("program") or f"{event.get('kind', 'unknown')}-expectation"
        result["expectations"][program] = {
            key: value for key, value in event.items()
            if key not in {"protocol", "protocol_version", "type"}
        }
    elif typ == "cache":
        result["cache"] = {
            key: value for key, value in event.items()
            if key not in {"protocol", "protocol_version", "type"}
        }
    elif typ == "final":
        result["final"] = {
            "ok": bool(event.get("ok")),
            "status": event.get("status", ""),
            "code": event.get("code", ""),
            "message": event.get("message", ""),
        }


def _sandbox_log_line(event):
    typ = event.get("type")
    if typ == "limits":
        return (
            f"[limits:{event.get('judge_type', 'standard')}] "
            f"{event.get('time_limit', 1):g}s / {event.get('memory_limit', 256)}MB"
        )
    if typ == "groups":
        names = [group.get("name") for group in (event.get("groups") or [])]
        return f"[groups] {', '.join(name for name in names if name) or 'none'}"
    if typ == "compile":
        ok = event.get("ok")
        status = "SKIP" if ok is None else ("OK" if ok else "FAIL")
        return f"[compile:{status}] {event.get('kind')} · {event.get('file')}"
    if typ == "validator":
        return f"[validator:{'OK' if event.get('ok') else 'FAIL'}] {event.get('case')}"
    if typ == "case":
        detail = f" · {event.get('message')}" if event.get("message") else ""
        return (
            f"[{event.get('kind')}:{event.get('judge_type', 'standard')}] "
            f"{event.get('program')} / {event.get('case')} -> {event.get('status')} "
            f"({float(event.get('time') or 0):.3f}s){detail}"
        )
    if typ == "transcript":
        suffix = " truncated" if event.get("truncated") else ""
        return (
            f"[transcript{suffix}] {event.get('program')} / {event.get('case')}: "
            f"{len(event.get('entries') or [])} chunks"
        )
    if typ == "summary":
        return f"[summary] {event.get('program')}: {event.get('stats')}"
    if typ == "expectation":
        state = "PASS" if event.get("ok") else "FAIL"
        first = event.get("first_forbidden") or event.get("first_expected_match") or event.get("first_non_ac")
        suffix = f" · first={first.get('case')}:{first.get('status')}" if first else ""
        return (
            f"[expectation:{state}] {event.get('program')} "
            f"status={event.get('expected_statuses') or []} "
            f"groups={event.get('groups') or ['all']}{suffix}"
        )
    if typ == "final":
        return f"[final:{'OK' if event.get('ok') else 'FAIL'}] {event.get('message')}"
    return json.dumps(event, ensure_ascii=False)


def _run_sandbox_job(job_id, subtitle, index):
    try:
        info = _sandbox_problem_info(subtitle, index)
        result = _empty_sandbox_result(info)
        with SANDBOX_LOCK:
            SANDBOX_JOBS[job_id]["result"] = result
            SANDBOX_JOBS[job_id]["logs"] = ""

        if not info.get("runnable"):
            message = info.get("reason") or "problem is not runnable"
            result["final"] = {"ok": False, "message": message}
            with SANDBOX_LOCK:
                SANDBOX_JOBS[job_id].update(status="failed", result=result, logs=message)
            return

        script_path = os.path.join("scripts", "local_judge.py")
        proc = subprocess.Popen(
            [sys.executable, script_path, info["dir"], "--jsonl"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        logs = []
        assert proc.stdout is not None
        for line in proc.stdout:
            raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
                _apply_sandbox_event(result, event)
                logs.append(_sandbox_log_line(event))
            except json.JSONDecodeError:
                logs.append(raw)
            with SANDBOX_LOCK:
                SANDBOX_JOBS[job_id]["logs"] = "\n".join(logs)
                SANDBOX_JOBS[job_id]["result"] = result

        return_code = proc.wait()
        final = result.get("final") or {}
        final_ok = bool(final.get("ok")) and return_code == 0
        with SANDBOX_LOCK:
            SANDBOX_JOBS[job_id]["status"] = "success" if final_ok else "failed"
            SANDBOX_JOBS[job_id]["logs"] = "\n".join(logs)
            SANDBOX_JOBS[job_id]["result"] = result
    except Exception as e:
        with SANDBOX_LOCK:
            SANDBOX_JOBS[job_id].update(status="failed", logs=str(e), result={"final": {"ok": False, "message": str(e)}})


@app.route('/api/sandbox/problem')
def sandbox_problem():
    try:
        subtitle = request.args.get("subtitle")
        index = int(request.args.get("index", "-1"))
        if not subtitle:
            return jsonify({"success": False, "error": "Missing subtitle"})
        return jsonify({"success": True, "info": _sandbox_problem_info(subtitle, index)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/sandbox/run', methods=['POST'])
def sandbox_run():
    try:
        payload = request.json or {}
        subtitle = payload.get("subtitle")
        index = int(payload.get("index", -1))
        if not subtitle:
            return jsonify({"success": False, "error": "Missing subtitle"})
        job_id = uuid.uuid4().hex
        with SANDBOX_LOCK:
            SANDBOX_JOBS[job_id] = {"status": "running", "logs": "", "result": None}
        Thread(target=_run_sandbox_job, args=(job_id, subtitle, index), daemon=True).start()
        return jsonify({"success": True, "job_id": job_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/sandbox/job/<job_id>')
def sandbox_job(job_id):
    with SANDBOX_LOCK:
        job = SANDBOX_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "job not found"}), 404
        return jsonify({"success": True, **job})


def _submission_verdict(result):
    submission_compiles = [item for item in (result.get("compiles") or []) if item.get("kind") == "std"]
    if submission_compiles and not submission_compiles[-1].get("ok"):
        return "CE"
    statuses = [item.get("status") for item in (result.get("cases") or []) if item.get("status")]
    if statuses and all(status == "AC" for status in statuses):
        return "AC"
    for status in ("FAIL", "RE", "MLE", "OLE", "TLE", "WA"):
        if status in statuses:
            return status
    return "FAIL" if result.get("final") else "PENDING"


def _prune_submission_jobs():
    now = time.time()
    expired = [
        job_id for job_id, job in SUBMISSION_JOBS.items()
        if job.get("status") not in {"queued", "running", "cancelling"}
        and now - float(job.get("finished_at") or job.get("created_at") or now) >= SUBMISSION_RESULT_TTL
    ]
    for job_id in expired:
        SUBMISSION_JOBS.pop(job_id, None)
    if len(SUBMISSION_JOBS) < 100:
        return
    finished = sorted(
        (
            (job_id, job)
            for job_id, job in SUBMISSION_JOBS.items()
            if job.get("status") not in {"queued", "running", "cancelling"}
        ),
        key=lambda item: item[1].get("created_at", 0),
    )
    for job_id, _ in finished[: max(1, len(SUBMISSION_JOBS) - 99)]:
        SUBMISSION_JOBS.pop(job_id, None)


def _force_cancel_submission(job_id, proc, known_pids):
    time.sleep(SUBMISSION_FORCE_CANCEL_AFTER)
    with SUBMISSION_LOCK:
        job = SUBMISSION_JOBS.get(job_id)
        still_cancelling = bool(job and job.get("status") in {"running", "cancelling"})
    if still_cancelling and proc.poll() is None:
        terminate_external_process_tree(proc, known_pids)


def _request_submission_cancel(job_id, proc, cancel_file):
    if cancel_file is not None:
        try:
            Path(cancel_file).touch(exist_ok=True)
        except OSError:
            pass
    if proc is None or proc.poll() is not None:
        return
    known_pids = snapshot_process_tree(proc.pid)
    try:
        if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass
    Thread(
        target=_force_cancel_submission,
        args=(job_id, proc, known_pids),
        daemon=True,
    ).start()


def _run_submission_job(job_id, subtitle, index, filename, source):
    with SUBMISSION_SLOTS:
        with SUBMISSION_LOCK:
            job = SUBMISSION_JOBS.get(job_id)
            if not job:
                return
            if job.get("cancel_requested"):
                job.update(status="cancelled", verdict="CANCELLED", finished_at=time.time())
                return
            job["status"] = "running"
        result = None
        logs = []
        return_code = None
        try:
            info = _sandbox_problem_info(subtitle, index)
            result = _empty_sandbox_result(info)
            result["submission"] = {
                "task_id": job_id,
                "filename": filename,
                "workspace_cleaned": False,
            }
            with SUBMISSION_LOCK:
                SUBMISSION_JOBS[job_id]["result"] = result

            if not info.get("runnable"):
                raise ValueError(info.get("reason") or "problem is not runnable")

            with SUBMISSION_LOCK:
                if SUBMISSION_JOBS[job_id].get("cancel_requested"):
                    result["submission"].update(workspace_cleaned=True, verdict="CANCELLED")
                    SUBMISSION_JOBS[job_id].update(
                        status="cancelled",
                        verdict="CANCELLED",
                        result=result,
                        finished_at=time.time(),
                    )
                    return

            with temporary_submission_workspace(Path.cwd(), info["dir"], job_id, filename, source) as prepared:
                cancel_file = prepared.root / "cancel.requested"
                with SUBMISSION_LOCK:
                    if SUBMISSION_JOBS[job_id].get("cancel_requested"):
                        raise RuntimeError("submission cancelled before judge startup")
                command = [
                    sys.executable,
                    str(LOCAL_JUDGE_SCRIPT),
                    str(prepared.problem_dir),
                    "--jsonl",
                    "--no-cache",
                    "--cancellable",
                ]
                env = os.environ.copy()
                env["PYTHONUTF8"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                env["PROBHUB_CANCEL_FILE"] = str(cancel_file)
                popen_options = {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "cwd": str(Path.cwd()),
                    "env": env,
                }
                if os.name == "nt":
                    popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    popen_options["start_new_session"] = True
                proc = subprocess.Popen(command, **popen_options)
                with SUBMISSION_LOCK:
                    SUBMISSION_PROCESSES[job_id] = proc
                    SUBMISSION_CANCEL_FILES[job_id] = cancel_file
                    cancel_requested = bool(SUBMISSION_JOBS[job_id].get("cancel_requested"))
                if cancel_requested:
                    _request_submission_cancel(job_id, proc, cancel_file)

                assert proc.stdout is not None
                try:
                    for line in proc.stdout:
                        raw = line.strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                            _apply_sandbox_event(result, event)
                            logs.append(_sandbox_log_line(event))
                        except json.JSONDecodeError:
                            logs.append(raw)
                        with SUBMISSION_LOCK:
                            SUBMISSION_JOBS[job_id]["logs"] = "\n".join(logs)
                            SUBMISSION_JOBS[job_id]["result"] = result
                finally:
                    proc.stdout.close()
                return_code = proc.wait()

            result["submission"]["workspace_cleaned"] = True
            with SUBMISSION_LOCK:
                cancel_requested = bool(SUBMISSION_JOBS[job_id].get("cancel_requested"))
            cancelled = cancel_requested or (result.get("final") or {}).get("status") == "cancelled"
            verdict = "CANCELLED" if cancelled else _submission_verdict(result)
            result["submission"]["verdict"] = verdict
            with SUBMISSION_LOCK:
                SUBMISSION_JOBS[job_id].update(
                    status="cancelled" if cancelled else "completed",
                    verdict=verdict,
                    returncode=return_code,
                    logs="\n".join(logs),
                    result=result,
                    finished_at=time.time(),
                )
        except Exception as exc:
            with SUBMISSION_LOCK:
                cancel_requested = bool(SUBMISSION_JOBS.get(job_id, {}).get("cancel_requested"))
            status = "cancelled" if cancel_requested else "failed"
            verdict = "CANCELLED" if cancel_requested else "FAIL"
            if result is not None and result.get("submission"):
                result["submission"]["workspace_cleaned"] = True
                result["submission"]["verdict"] = verdict
            with SUBMISSION_LOCK:
                if job_id in SUBMISSION_JOBS:
                    SUBMISSION_JOBS[job_id].update(
                        status=status,
                        verdict=verdict,
                        logs=("\n".join(logs + ([] if cancel_requested else [str(exc)]))).strip(),
                        result=result or {"final": {"ok": False, "message": str(exc)}},
                        finished_at=time.time(),
                    )
        finally:
            with SUBMISSION_LOCK:
                SUBMISSION_PROCESSES.pop(job_id, None)
                SUBMISSION_CANCEL_FILES.pop(job_id, None)


@app.route('/api/submission/run', methods=['POST'])
def submission_run():
    try:
        if request.content_length and request.content_length > MAX_SUBMISSION_REQUEST_BYTES:
            return jsonify({"success": False, "error": "upload is too large"}), 413
        subtitle = request.form.get("subtitle", "")
        index = int(request.form.get("index", "-1"))
        upload = request.files.get("source")
        if not subtitle:
            return jsonify({"success": False, "error": "Missing subtitle"}), 400
        if upload is None:
            return jsonify({"success": False, "error": "Missing source file"}), 400
        filename = upload.filename or ""
        source = upload.stream.read(MAX_SOURCE_BYTES + 1)
        validate_cpp_upload(filename, source)
        # Resolve the selected problem before accepting the job. This prevents
        # retaining uploads for invalid or non-runnable selections.
        info = _sandbox_problem_info(subtitle, index)
        if not info.get("runnable"):
            return jsonify({"success": False, "error": info.get("reason") or "problem is not runnable"}), 400

        job_id = uuid.uuid4().hex
        with SUBMISSION_LOCK:
            _prune_submission_jobs()
            protected = [
                current_id for current_id, job in SUBMISSION_JOBS.items()
                if job.get("status") in {"queued", "running", "cancelling"}
            ]
            cleanup_stale_submission_workspaces(Path.cwd(), protected_task_ids=protected)
            SUBMISSION_JOBS[job_id] = {
                "status": "queued",
                "verdict": "PENDING",
                "logs": "",
                "result": None,
                "filename": filename,
                "created_at": time.time(),
                "cancel_requested": False,
            }
        Thread(
            target=_run_submission_job,
            args=(job_id, subtitle, index, filename, source),
            daemon=True,
        ).start()
        return jsonify({"success": True, "job_id": job_id, "status": "queued"})
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route('/api/submission/job/<job_id>')
def submission_job(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"success": False, "error": "invalid job id"}), 400
    with SUBMISSION_LOCK:
        job = SUBMISSION_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "job not found"}), 404
        return jsonify({"success": True, **job})


@app.route('/api/submission/job/<job_id>/cancel', methods=['POST'])
def submission_cancel(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"success": False, "error": "invalid job id"}), 400
    with SUBMISSION_LOCK:
        job = SUBMISSION_JOBS.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "job not found"}), 404
        if job.get("status") not in {"queued", "running", "cancelling"}:
            return jsonify({"success": True, "status": job.get("status"), "verdict": job.get("verdict")})
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        proc = SUBMISSION_PROCESSES.get(job_id)
        cancel_file = SUBMISSION_CANCEL_FILES.get(job_id)
    _request_submission_cancel(job_id, proc, cancel_file)
    return jsonify({"success": True, "status": "cancelling"})


def inject_pdf_to_zip(pdf_path, prob_dir):
    """将 problem.pdf 注入同名的 .zip 压缩包（如果存在）。"""
    import zipfile as zf
    base = os.path.basename(os.path.normpath(prob_dir))
    zip_path = os.path.join(base + ".zip")
    if not os.path.exists(zip_path):
        return None
    try:
        tmp = zip_path + ".tmp"
        with zf.ZipFile(zip_path, "r") as zin, zf.ZipFile(tmp, "w", compression=zf.ZIP_DEFLATED) as zout:
            infos = zin.infolist()
            arcname = _zip_preferred_arcname(infos, base, "problem.pdf")
            skip_names = _zip_candidate_names(base, "problem.pdf")
            for item in zin.infolist():
                if item.filename not in skip_names:
                    zout.writestr(item, zin.read(item.filename))
            zout.write(pdf_path, arcname)
        os.replace(tmp, zip_path)
        return "updated"
    except Exception as e:
        return f"error: {e}"


def distribute_problems(subtitle):
    """Run extract_new_problem.py for each problem in the current subtitle."""
    # Find the extract script (same dir as this ui.py or in skill scripts)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    extract_script = os.path.join(script_dir, "extract_new_problem.py")

    # If not in workspace, look in skill scripts
    if not os.path.exists(extract_script):
        skill_scripts = os.path.join(
            os.path.expanduser("~"), ".claude", "skills", "probhub", "scripts", "extract_new_problem.py"
        )
        if os.path.exists(skill_scripts):
            extract_script = skill_scripts
        else:
            return {"error": "extract_new_problem.py not found"}

    # Match problem names to directories
    name_to_dir = find_problem_dirs()

    # Read current problems.json to get active problems in order
    typst_subdir = os.path.join(BASE_DIR, subtitle)
    problems_json = os.path.join(typst_subdir, "problems.json")
    if not os.path.exists(problems_json):
        return {"error": "problems.json not found"}

    with open(problems_json, "r", encoding="utf-8") as f:
        problems = json.load(f)

    results = []
    for p in problems:
        display_name = p.get("problem", {}).get("display_name", "")
        if not display_name:
            results.append({"name": "?", "status": "skipped", "reason": "no display_name"})
            continue
        prob_dir = name_to_dir.get(display_name)
        if not prob_dir:
            results.append({"name": display_name, "status": "skipped", "reason": "no matching directory"})
            continue

        typst_dir = os.path.join(BASE_DIR, subtitle)
        try:
            subprocess.run(
                [sys.executable, extract_script, typst_dir, prob_dir],
                check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            # Inject problem.pdf into matching .zip if it exists
            pdf_path = os.path.join(prob_dir, "problem.pdf")
            zip_status = inject_pdf_to_zip(pdf_path, prob_dir) if os.path.exists(pdf_path) else None
            results.append({
                "name": display_name, "status": "ok", "dir": prob_dir,
                "zip": zip_status  # None=no zip, "updated"=done, "error:..."=fail
            })
        except subprocess.CalledProcessError as e:
            results.append({"name": display_name, "status": "failed", "reason": e.stderr[:200]})

    return results

# ── Contest Config (main.typ + lib.typ) ──────────────────────────

def _read_contest_config(subtitle):
    """Parse main.typ and lib.typ, return config dict."""
    main_path = secure_path(subtitle, "main.typ")
    lib_path = os.path.join(BASE_DIR, "lib.typ")
    config = {
        "title": "", "subtitle": subtitle, "author": "", "date": "",
        "logo": "usts.png", "logo_width": "9cm",
        "logo_space_above": "0em", "logo_space_below": "0em",
    }

    if os.path.exists(main_path):
        with open(main_path, "r", encoding="utf-8") as f:
            text = f.read()
        for key in ("title", "subtitle", "author", "date"):
            m = re.search(rf'{key}:\s*"([^"]*)"', text)
            if m:
                config[key] = m.group(1)

    if os.path.exists(lib_path):
        with open(lib_path, "r", encoding="utf-8") as f:
            text = f.read()
        m = re.search(r'image\("([^"]+)"\s*(?:,\s*width:\s*([^,)]+))?', text)
        if m:
            config["logo"] = m.group(1)
            if m.group(2):
                config["logo_width"] = m.group(2).strip()
        # Parse space above/below logo
        # Look for v(Xem) on the line immediately before/after align(center, image(...))
        m = re.search(r'v\(([^)]+)\)\s*(?://[^\n]*)?\s*\n\s*(?://[^\n]*\n\s*)?align\(center,\s*image\(', text)
        if m:
            config["logo_space_above"] = m.group(1).strip()
        m = re.search(r'align\(center,\s*image\([^)]+\)\)\s*\n\s*v\(([^)]+)\)', text)
        if m:
            config["logo_space_below"] = m.group(1).strip()

    return config


def _write_contest_config(subtitle, config):
    """Write updated values back to main.typ and lib.typ."""
    main_path = secure_path(subtitle, "main.typ")
    lib_path = os.path.join(BASE_DIR, "lib.typ")

    if os.path.exists(main_path):
        with open(main_path, "r", encoding="utf-8") as f:
            text = f.read()
        for key in ("title", "subtitle", "author", "date"):
            if key in config:
                text = re.sub(rf'({key}:\s*)"[^"]*"', rf'\1"{config[key]}"', text)
        with open(main_path, "w", encoding="utf-8") as f:
            f.write(text)

    if os.path.exists(lib_path):
        with open(lib_path, "r", encoding="utf-8") as f:
            text = f.read()
        logo = config.get("logo", "usts.png")
        width = config.get("logo_width", "9cm")
        space_above = config.get("logo_space_above", "0em")
        space_below = config.get("logo_space_below", "0em")
        # Update image path/width
        text = re.sub(r'image\("[^"]+"\s*(?:,\s*width:\s*[^,)]+)?', f'image("{logo}", width: {width}', text)
        # Update space above logo (v() before align(center, image())
        text = re.sub(
            r'v\([^)]+\)(\s*(?://[^\n]*)?\s*\n\s*(?://[^\n]*\n\s*)?align\(center,\s*image\()',
            f'v({space_above})\\1', text
        )
        # Update space below logo (v() after image line)
        text = re.sub(
            r'(align\(center,\s*image\([^)]+\)\)\s*\n\s*)v\([^)]+\)',
            f'\\1v({space_below})', text
        )
        with open(lib_path, "w", encoding="utf-8") as f:
            f.write(text)


@app.route('/api/config/<subtitle>', methods=['GET'])
def get_contest_config(subtitle):
    try:
        defaults = _read_contest_config(subtitle)
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, workspace = schema
            return jsonify({"success": True, "config": load_contest_config(root, workspace, defaults)})
        return jsonify({"success": True, "config": defaults})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/config/<subtitle>', methods=['POST'])
def save_contest_config(subtitle):
    try:
        config = request.json
        schema = _schema_workspace(subtitle)
        if schema is not None:
            root, _ = schema
            with workspace_build_lock(root):
                _, live_workspace = load_workspace(root)
                saved = save_schema_contest_config(root, live_workspace, config)
            return jsonify({"success": True, "config": saved})
        _write_contest_config(subtitle, config)
        return jsonify({"success": True})
    except ProbHubError as e:
        status = 409 if e.code == "source_conflict" else 400
        return jsonify({"success": False, "error": str(e), "code": e.code}), status
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/pdf-pages/<subtitle>')
def pdf_page_count(subtitle):
    """Return the number of pages in main.pdf."""
    import pypdf
    preview_path = _preview_pdf_path(subtitle)
    pdf_path = str(preview_path if preview_path.is_file() else Path(secure_path(subtitle, "main.pdf")))
    if not os.path.exists(pdf_path):
        return jsonify({"pages": 0})
    try:
        reader = pypdf.PdfReader(pdf_path)
        return jsonify({"pages": len(reader.pages)})
    except Exception:
        return jsonify({"pages": 0})


@app.route('/api/pdf-page/<subtitle>/<int:page>')
def serve_pdf_page(subtitle, page):
    """Render a single PDF page through Poppler into the process temp cache."""
    from flask import send_file
    import pypdf

    preview_path = _preview_pdf_path(subtitle)
    pdf_path = str(preview_path if preview_path.is_file() else Path(secure_path(subtitle, "main.pdf")))
    if not os.path.exists(pdf_path):
        return "PDF not compiled", 404

    # Validate page number
    try:
        reader = pypdf.PdfReader(pdf_path)
        total = len(reader.pages)
        if page < 0 or page >= total:
            return "Page out of range", 404
    except Exception:
        return "Invalid PDF", 500

    # Page rendering is a read-only WebUI concern; never cache into the workspace.
    preview_dir = str(WEBUI_PREVIEW_ROOT / subtitle / ".pages")
    os.makedirs(preview_dir, exist_ok=True)
    png_path = os.path.join(preview_dir, f"page-{page + 1}.png")

    # Regenerate if PNG is missing or older than PDF
    if not os.path.exists(png_path) or os.path.getmtime(png_path) < os.path.getmtime(pdf_path):
        prefix = str(Path(png_path).with_suffix(""))
        stdout_path = Path(preview_dir) / f"page-{page + 1}.stdout"
        stderr_path = Path(preview_dir) / f"page-{page + 1}.stderr"
        try:
            rendered = run_managed_to_files(
                [
                    "pdftoppm",
                    "-f", str(page + 1),
                    "-l", str(page + 1),
                    "-singlefile",
                    "-png",
                    "-r", "144",
                    pdf_path,
                    prefix,
                ],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=30,
                memory_limit_mb=512,
                output_limit_bytes=4 * 1024 * 1024,
                process_limit=8,
                cwd=Path.cwd(),
            )
        except OSError as exc:
            return f"Render failed: {exc}", 500
        if rendered["reason"] != "completed" or rendered["returncode"] != 0:
            detail = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
            return f"Render failed: {detail[-1000:]}", 500

    if not os.path.exists(png_path):
        return "Render failed", 500

    return send_file(os.path.abspath(png_path), mimetype='image/png')


def open_browser():
    webbrowser.open_new("http://127.0.0.1:33933")

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀 ProbHub 动态排版控制台启动...")
    print(f"📂 正在监听基础目录: {BASE_DIR}/")
    print("="*50)
    Timer(1, open_browser).start()
    app.run(host='127.0.0.1', port=33933, debug=False)
