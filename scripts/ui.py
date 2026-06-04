# -*- coding: utf-8 -*-
import os
import sys
import json
import re
import subprocess
import webbrowser
from threading import Timer
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

BASE_DIR = "typst-statement"

def secure_path(subtitle, filename):
    """安全路径拼接，防止路径穿越攻击"""
    if not subtitle or '..' in subtitle or '/' in subtitle or '\\' in subtitle:
        raise ValueError("Invalid subtitle")
    return os.path.join(BASE_DIR, subtitle, filename)

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
    <link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Inter:wght@350;400;500;600&display=swap" rel="stylesheet">
    
    <script src="https://cdn.tailwindcss.com?plugins=typography"></script>
    <script>
      tailwind = { 
        config: {
          theme: {
            extend: {
              colors: {
                ink:      { bg:'#0b0d12', card:'#13151c', elevated:'#181b24', input:'#1e212b', border:'#252832' },
                gold:     { DEFAULT:'#c8a45c', light:'#d4b670', muted:'#9e8040', dim:'#6b5a38' },
                cream:    { DEFAULT:'#e4dfd4', muted:'#98968e', subtle:'#5f5e59' },
                success:  { DEFAULT:'#6b9b6a', bg:'rgba(107,155,106,0.10)' },
                danger:   { DEFAULT:'#c25450', bg:'rgba(194,84,80,0.10)' },
              },
              fontFamily: {
                serif: ['"Noto Serif SC"', '"Noto Serif"', 'Georgia', 'serif'],
                mono:  ['"JetBrains Mono"', '"Cascadia Code"', 'Consolas', 'monospace'],
                sans:  ['"Inter"', 'system-ui', 'sans-serif'],
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
        :root { color-scheme: dark; }
        * { scrollbar-width: thin; scrollbar-color: #252832 transparent; }
        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #252832; border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: #353843; }
        html, body { background: #0b0d12; }
        body::before {
            content: ''; position: fixed; inset: 0; pointer-events: none; z-index: 0;
            background: radial-gradient(ellipse 80% 50% at 50% -10%, rgba(200,164,92,0.04) 0%, transparent 70%),
                        radial-gradient(ellipse 60% 40% at 90% 80%, rgba(200,164,92,0.02) 0%, transparent 70%);
        }
        .drag-ghost { opacity: 0.25; background: #1e212b !important; border: 1px dashed #c8a45c !important; }
        [x-cloak] { display: none !important; }
        .ink-card { background: #13151c; border: 1px solid rgba(255,255,255,0.025); backdrop-filter: blur(12px); }
        .gold-glow:focus-within { box-shadow: 0 0 0 1px rgba(200,164,92,0.4), 0 0 12px rgba(200,164,92,0.08); }
        .toggle-track { transition: background-color 0.25s ease; }
        .toggle-dot { transition: transform 0.25s cubic-bezier(0.34,1.56,0.64,1); }
        
        .prose mjx-container { outline: none !important; }
        .prose mjx-container svg { max-width: none !important; height: auto !important; display: inline !important; }
        mjx-container:not([display="true"]) { display: inline-block !important; margin: 0 !important; }
        mjx-container[display="true"] { margin: 0.75em 0 !important; }
        
        /* 下拉菜单定制样式 */
        select option { background-color: #13151c; color: #e4dfd4; }
    </style>
</head>
<body class="bg-ink-bg text-cream antialiased relative">
    <div x-data="probhub()" x-init="initApp()" @input="autoSave()" class="max-w-7xl mx-auto px-6 py-6 relative z-10" x-cloak>

        <div class="ink-card rounded-2xl p-5 mb-5 flex justify-between items-center">
            <div class="flex items-center gap-5">
                <svg class="h-12 w-auto select-none shrink-0" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 324 90">
                    <style>.logo-text { font-family: "New Computer Modern Mono", "Courier New", monospace; font-weight: bold; font-size: 64px; }</style>
                    <rect width="324" height="90" rx="16" fill="#0b0d12" />
                    <text x="22" y="64" class="logo-text" fill="#E53935">p</text>
                    <text x="58" y="64" class="logo-text" fill="#E53935">r</text>
                    <text x="94" y="64" class="logo-text" fill="#E53935">o</text>
                    <text x="130" y="64" class="logo-text" fill="#1E88E5">b</text>
                    <rect x="182" y="14" width="130" height="62" rx="10" fill="#FFA31A" />
                    <text x="192" y="64" class="logo-text" fill="#0b0d12">h</text>
                    <text x="228" y="64" class="logo-text" fill="#0b0d12">u</text>
                    <text x="264" y="64" class="logo-text" fill="#0b0d12">b</text>
                </svg>
                <div class="flex flex-col gap-1">
                    <span class="text-[11px] tracking-[0.2em] uppercase text-cream-muted font-medium">Typesetting Console</span>
                    <div class="flex items-center gap-2">
                        <span class="text-sm text-cream-subtle">当前排版集</span>
                        <div class="relative flex items-center" x-show="subtitles.length > 0">
                            <select x-model="currentSubtitle" @change="switchSubtitle()"
                                    class="bg-ink-input text-gold font-mono text-[13px] border border-white/[0.02] rounded-md pl-2.5 pr-8 py-1 outline-none focus:border-gold/40 cursor-pointer appearance-none transition-colors shadow-sm">
                                <template x-for="sub in subtitles" :key="sub">
                                    <option :value="sub" x-text="sub"></option>
                                </template>
                            </select>
                            <svg class="w-3.5 h-3.5 text-gold/60 absolute right-2.5 pointer-events-none" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
                        </div>
                        <span x-show="subtitles.length === 0" class="text-[13px] text-danger font-mono bg-danger/10 px-2 py-0.5 rounded border border-danger/20">未检测到排版集</span>
                    </div>
                </div>
            </div>

            <div class="flex items-center gap-3">
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
                <button @click="compilePDF()" class="px-5 py-2.5 text-[13px] font-semibold rounded-lg transition-all duration-200 bg-gradient-to-b from-[#d4b468] to-[#b8923e] text-[#1a1408] shadow-[0_0_20px_rgba(200,164,92,0.25)] hover:shadow-[0_0_28px_rgba(200,164,92,0.40)] active:scale-[0.97] disabled:opacity-50 disabled:shadow-none" :disabled="isCompiling || !currentSubtitle">
                    <span class="flex items-center gap-1.5">
                        <span x-show="!isCompiling">📄 编译全卷</span>
                        <span x-show="isCompiling" class="animate-pulse">⏳ 编译中...</span>
                    </span>
                </button>
                <button @click="distributePDFs()" class="px-4 py-2.5 text-[13px] font-medium rounded-lg transition-all duration-200 bg-ink-elevated border border-white/[0.03] text-cream-muted hover:text-cream hover:border-white/8 hover:bg-ink-input active:scale-[0.97] disabled:opacity-40" :disabled="isDistributing || !currentSubtitle">
                    <span class="flex items-center gap-1.5">
                        <span x-show="!isDistributing">📦 分发 PDF</span>
                        <span x-show="isDistributing" class="animate-pulse">⏳ 分发中...</span>
                    </span>
                </button>
            </div>
        </div>

        <div x-show="toast.show" x-transition.opacity.duration.300ms class="fixed top-6 right-6 z-50 flex items-center gap-2 px-5 py-3 rounded-xl text-sm font-medium shadow-2xl border backdrop-blur-md" :class="toast.isError ? 'bg-danger/90 border-red-500/30 text-white' : 'bg-success/90 border-green-500/30 text-white'">
            <span x-text="toast.msg"></span>
        </div>

        <div class="flex gap-5" style="height:calc(100vh - 140px);">

            <div class="w-[220px] shrink-0 ink-card rounded-2xl p-4 flex flex-col overflow-hidden">
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

            <div class="flex-1 ink-card rounded-2xl p-6 overflow-y-auto relative">
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
                                    <h3 class="text-[11px] font-medium tracking-wide text-cream-muted uppercase">封面设置 <span class="text-[9px] text-cream-subtle font-normal tracking-normal normal-case">（编译后可预览）</span></h3>
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
                            <div class="mt-3" x-show="pdfPages.length > 0">
                                <p class="text-[10px] font-medium text-cream-subtle mb-2">封面预览</p>
                                <div class="rounded-lg overflow-hidden border border-white/[0.02] bg-ink-bg">
                                    <img :src="'/api/pdf-page/' + encodeURIComponent(currentSubtitle) + '/0?t=' + pdfRefresh"
                                         class="w-full" style="filter: brightness(0.92) contrast(0.95)">
                                </div>
                            </div>
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
                                         :style="'left:calc(' + (li / 5 * 100) + '% - 7px); background:' + (getDifficulty(selectedIdx) >= li ? level.color : '#1e212b') + '; border: 2px solid ' + (getDifficulty(selectedIdx) >= li ? level.color : '#3a3d48') + ';' + (getDifficulty(selectedIdx) === li ? 'transform: scale(1.7); box-shadow: 0 0 12px ' + level.color + '99;' : '')"
                                         @click.stop="setDifficulty(selectedIdx, li)"></div>
                                </template>
                            </div>
                            <!-- Labels -->
                            <div class="flex justify-between mt-1">
                                <template x-for="(level, li) in difficultyLevels" :key="level.label">
                                    <span class="text-[9px] font-medium transition-colors duration-300 select-none"
                                          :style="getDifficulty(selectedIdx) === li ? 'color:' + level.color : 'color:#5f5e59'"
                                          x-text="level.label"></span>
                                </template>
                            </div>
                        </div>

                        <!-- Quote expandable section -->
                        <template x-if="hasQuote()">
                            <div x-show="true" x-collapse>
                                <div class="p-4 rounded-xl space-y-4 border-l-2 border-gold/30" style="background:rgba(200,164,92,0.04);">
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
                                <div class="grid grid-cols-2 gap-4 mb-2">
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full bg-gold"></div> Description</label>
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full opacity-0"></div> Live Preview</label>
                                </div>
                                <div class="grid grid-cols-2 gap-4">
                                    <textarea x-model="problems[selectedIdx].statement.description" rows="8" class="w-full h-56 px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg font-mono text-[13px] text-cream placeholder-cream-subtle/40 leading-relaxed focus:border-gold/40 focus:outline-none transition-colors resize-none"></textarea>
                                    <div class="w-full h-56 px-4 py-3 bg-ink-elevated border border-white/[0.02] rounded-lg overflow-y-auto prose prose-invert max-w-none text-[13.5px] leading-relaxed prose-p:my-1.5 prose-p:leading-normal prose-ul:my-1.5 prose-li:my-0.5 prose-pre:bg-ink-card prose-pre:my-2 prose-a:text-gold" x-effect="renderMath($refs.descPreview, problems[selectedIdx]?.statement?.description)" x-ref="descPreview"></div>
                                </div>
                            </div>

                            <div>
                                <div class="grid grid-cols-2 gap-4 mb-2">
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full bg-gold"></div> Input Format</label>
                                </div>
                                <div class="grid grid-cols-2 gap-4">
                                    <textarea x-model="problems[selectedIdx].statement.input" class="w-full h-32 px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg font-mono text-[13px] text-cream leading-relaxed focus:border-gold/40 focus:outline-none transition-colors resize-none"></textarea>
                                    <div class="w-full h-32 px-4 py-3 bg-ink-elevated border border-white/[0.02] rounded-lg overflow-y-auto prose prose-invert max-w-none text-[13.5px] leading-relaxed prose-p:my-1.5 prose-p:leading-normal prose-ul:my-1.5 prose-li:my-0.5 prose-pre:bg-ink-card prose-pre:my-2 prose-a:text-gold" x-effect="renderMath($refs.inputPreview, problems[selectedIdx]?.statement?.input)" x-ref="inputPreview"></div>
                                </div>
                            </div>

                            <div>
                                <div class="grid grid-cols-2 gap-4 mb-2">
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full bg-gold"></div> Output Format</label>
                                </div>
                                <div class="grid grid-cols-2 gap-4">
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
                                            <div class="p-4 rounded-xl border border-white/[0.02]" style="background:rgba(255,255,255,0.015);">
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
                                <div class="grid grid-cols-2 gap-4 mb-2">
                                    <label class="text-[11px] font-medium tracking-wide text-cream-muted uppercase flex items-center gap-2"><div class="w-1 h-3.5 rounded-full bg-gold"></div> Notes & Constraints</label>
                                </div>
                                <div class="grid grid-cols-2 gap-4">
                                    <textarea x-model="problems[selectedIdx].statement.notes" class="w-full h-32 px-3.5 py-2.5 bg-ink-input border border-white/[0.03] rounded-lg font-mono text-[13px] text-cream leading-relaxed focus:border-gold/40 focus:outline-none transition-colors resize-none"></textarea>
                                    <div class="w-full h-32 px-4 py-3 bg-ink-elevated border border-white/[0.02] rounded-lg overflow-y-auto prose prose-invert max-w-none text-[13.5px] leading-relaxed prose-p:my-1.5 prose-p:leading-normal prose-ul:my-1.5 prose-li:my-0.5 prose-pre:bg-ink-card prose-pre:my-2 prose-a:text-gold" x-effect="renderMath($refs.notesPreview, problems[selectedIdx]?.statement?.notes)" x-ref="notesPreview"></div>
                                </div>
                            </div>

                        </div>
                    </div>
                </template>
            </div>

        </div>
    </div>

    <script>
        document.addEventListener('alpine:init', () => {
            Alpine.data('probhub', () => ({
                subtitles: [],
                currentSubtitle: '',
                problems: [],
                selectedIdx: null,
                isCompiling: false,
                isDistributing: false,
                pdfRefresh: Date.now(),
                pdfPages: [],
                trackWidth: 800,
                saveStatus: '',   // '' | 'saving' | 'saved' | 'error'
                tagDraft: '',
                coverConfig: { title: '', subtitle: '', author: '', date: '', logo: 'usts.png', logo_width: '9cm', logo_space_above: '0em', logo_space_below: '0em' },
                _saveTimer: null,
                _coverSaveTimer: null,
                toast: { show: false, msg: '', isError: false },

                initApp() {
                    // 1. 初始化时拉取所有可用的排版集目录
                    fetch('/api/subtitles').then(res => res.json()).then(subs => {
                        this.subtitles = subs;
                        if (subs.length > 0) {
                            // 默认选择第一个
                            this.currentSubtitle = subs[0];
                            this.loadData();
                            this.loadConfig();
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
                            this.$nextTick(() => { this.initSortable(); });
                        });
                },

                loadPdfPages() {
                    if (!this.currentSubtitle) { this.pdfPages = []; return; }
                    fetch(`/api/pdf-pages/${encodeURIComponent(this.currentSubtitle)}`)
                        .then(res => res.json())
                        .then(data => {
                            this.pdfPages = data.pages > 0 ? Array.from({length: data.pages}, (_, i) => i) : [];
                        });
                },

                switchSubtitle() {
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
                    }).then(res => res.json()).then(data => {
                        if (data.success) { /* silent */ }
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

                selectProb(index) { this.selectedIdx = index; this.tagDraft = ''; },
                
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
                    this._saveTimer = setTimeout(() => this._doSave(), 800);
                },

                _doSave() {
                    if (!this.currentSubtitle) return;
                    this.saveStatus = 'saving';
                    fetch('/api/data', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ subtitle: this.currentSubtitle, problems: this.problems })
                    }).then(res => res.json()).then(data => {
                        if (data.success) {
                            this.saveStatus = 'saved';
                            setTimeout(() => { if (this.saveStatus === 'saved') this.saveStatus = ''; }, 2500);
                        } else {
                            this.saveStatus = 'error';
                        }
                    }).catch(() => { this.saveStatus = 'error'; });
                },

                doSave() {
                    clearTimeout(this._saveTimer);
                    return this._doSave();
                },

                compilePDF() {
                    if (!this.currentSubtitle) return;
                    clearTimeout(this._saveTimer);
                    this._doSave();
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
                            this.showToast('Compile failed', true);
                        }
                    });
                },

                distributePDFs() {
                    if (!this.currentSubtitle) return;
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

                showToast(msg, isError = false) {
                    this.toast = { show: true, msg, isError };
                    setTimeout(() => { this.toast.show = false; }, 3000);
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
        json_path = secure_path(subtitle, "problems.json")
        if not os.path.exists(json_path):
            return jsonify([])
        with open(json_path, 'r', encoding='utf-8') as f:
            return jsonify(json.load(f))
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
        json_path = secure_path(subtitle, "problems.json")
        os.makedirs(os.path.dirname(json_path), exist_ok=True)
        tmp_path = json_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, json_path)  # atomic replace
        return jsonify({"success": True})
    except Exception as e:
        print(f"[-] Save Data Error: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/compile', methods=['POST'])
def compile_pdf():
    """仅编译 typst，不分发 PDF。"""
    payload = request.json
    subtitle = payload.get('subtitle')
    if not subtitle:
        return jsonify({"success": False, "error": "Missing subtitle"})
    try:
        typst_main = secure_path(subtitle, "main.typ")
        subprocess.run(["typst", "compile", "--root", ".", typst_main],
                       check=True, text=True, encoding='utf-8', errors='replace')
        return jsonify({"success": True})
    except subprocess.CalledProcessError as e:
        print(f"[-] Typst compile failed: {e.stderr}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/distribute', methods=['POST'])
def distribute_pdfs():
    """仅分发单题 PDF + 注入同名 zip，不编译。"""
    payload = request.json
    subtitle = payload.get('subtitle')
    if not subtitle:
        return jsonify({"success": False, "error": "Missing subtitle"})
    try:
        dist_results = distribute_problems(subtitle)
        return jsonify({"success": True, "distributed": dist_results})
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


def inject_pdf_to_zip(pdf_path, prob_dir):
    """将 problem.pdf 注入同名的 .zip 压缩包（如果存在）。"""
    import zipfile as zf
    base = os.path.basename(os.path.normpath(prob_dir))
    zip_path = os.path.join(base + ".zip")
    if not os.path.exists(zip_path):
        return None
    try:
        arcname = f"{base}/problem.pdf"
        tmp = zip_path + ".tmp"
        with zf.ZipFile(zip_path, "r") as zin, zf.ZipFile(tmp, "w", compression=zf.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename != arcname:
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
        return jsonify({"success": True, "config": _read_contest_config(subtitle)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/config/<subtitle>', methods=['POST'])
def save_contest_config(subtitle):
    try:
        config = request.json
        _write_contest_config(subtitle, config)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/pdf-pages/<subtitle>')
def pdf_page_count(subtitle):
    """Return the number of pages in main.pdf."""
    import pypdf
    pdf_path = secure_path(subtitle, "main.pdf")
    if not os.path.exists(pdf_path):
        return jsonify({"pages": 0})
    try:
        reader = pypdf.PdfReader(pdf_path)
        return jsonify({"pages": len(reader.pages)})
    except Exception:
        return jsonify({"pages": 0})


@app.route('/api/pdf-page/<subtitle>/<int:page>')
def serve_pdf_page(subtitle, page):
    """Render a single page of main.pdf as PNG using typst, then serve it."""
    from flask import send_file
    import pypdf

    pdf_path = secure_path(subtitle, "main.pdf")
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

    # Cache PNG in .preview/ directory
    preview_dir = os.path.join(BASE_DIR, subtitle, ".preview")
    os.makedirs(preview_dir, exist_ok=True)
    png_path = os.path.join(preview_dir, f"page-{page + 1}.png")

    # Regenerate if PNG is missing or older than PDF
    if not os.path.exists(png_path) or os.path.getmtime(png_path) < os.path.getmtime(pdf_path):
        typst_main = secure_path(subtitle, "main.typ")
        subprocess.run(
            ["typst", "compile", "--root", ".", "--format", "png",
             f"--pages={page + 1}", typst_main, png_path],
            check=True, capture_output=True,
            text=True, encoding='utf-8', errors='replace'
        )

    if not os.path.exists(png_path):
        return "Render failed", 500

    return send_file(png_path, mimetype='image/png')


def open_browser():
    webbrowser.open_new("http://127.0.0.1:33933")

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🚀 ProbHub 动态排版控制台启动...")
    print(f"📂 正在监听基础目录: {BASE_DIR}/")
    print("="*50)
    Timer(1, open_browser).start()
    app.run(host='127.0.0.1', port=33933, debug=False)