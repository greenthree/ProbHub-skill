        const PROBHUB_CSRF_TOKEN = document.querySelector('meta[name="probhub-csrf-token"]').content;
        const PROBHUB_MARKDOWN_TAGS = new Set([
            'a', 'blockquote', 'br', 'code', 'del', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
            'hr', 'img', 'kbd', 'li', 'ol', 'p', 'pre', 'span', 'strong', 'sub', 'sup', 'table',
            'tbody', 'td', 'th', 'thead', 'tr', 'ul'
        ]);
        const PROBHUB_MARKDOWN_DROP_CONTENT = new Set([
            'audio', 'base', 'canvas', 'embed', 'form', 'iframe', 'math', 'noscript', 'object',
            'script', 'style', 'svg', 'template', 'video', 'xmp'
        ]);
        const PROBHUB_MARKDOWN_ATTRIBUTES = {
            a: new Set(['href', 'title', 'target', 'rel']),
            code: new Set(['class']),
            img: new Set(['src', 'alt', 'title']),
            ol: new Set(['start']),
            td: new Set(['align']),
            th: new Set(['align'])
        };

        function markdownUrlIsSafe(value, attribute) {
            try {
                const parsed = new URL(String(value || '').trim(), window.location.origin);
                if (attribute === 'href') return new Set(['http:', 'https:', 'mailto:']).has(parsed.protocol);
                return parsed.origin === window.location.origin && new Set(['http:', 'https:']).has(parsed.protocol);
            } catch (_) {
                return false;
            }
        }

        function sanitizeRenderedMarkdown(html) {
            const template = document.createElement('template');
            template.innerHTML = String(html || '');
            for (const node of Array.from(template.content.querySelectorAll('*'))) {
                const tag = node.localName;
                if (PROBHUB_MARKDOWN_DROP_CONTENT.has(tag)) {
                    node.remove();
                    continue;
                }
                if (!PROBHUB_MARKDOWN_TAGS.has(tag)) {
                    node.replaceWith(...node.childNodes);
                    continue;
                }
                const allowed = PROBHUB_MARKDOWN_ATTRIBUTES[tag] || new Set();
                for (const attribute of Array.from(node.attributes)) {
                    const name = attribute.name.toLowerCase();
                    if (!allowed.has(name) || name.startsWith('on') || name === 'style' || name === 'srcdoc') {
                        node.removeAttribute(attribute.name);
                        continue;
                    }
                    if ((name === 'href' || name === 'src') && !markdownUrlIsSafe(attribute.value, name)) {
                        node.removeAttribute(attribute.name);
                    }
                }
                if (tag === 'a') {
                    const target = node.getAttribute('target');
                    if (target && target !== '_blank' && target !== '_self') node.removeAttribute('target');
                    if (node.getAttribute('target') === '_blank') node.setAttribute('rel', 'noopener noreferrer');
                }
            }
            return template.content;
        }

        document.addEventListener('alpine:init', () => {
            Alpine.data('probhub', () => ({
                theme: document.documentElement.dataset.theme || 'dark',
                subtitles: [],
                currentSubtitle: '',
                problems: [],
                selectedIdx: null,
                activePage: 'layout',
                health: null,
                healthLoading: false,
                healthError: '',
                coverage: null,
                coverageLoading: false,
                coverageError: '',
                isCompiling: false,
                isDistributing: false,
                sandboxRunning: false,
                sandboxCancelling: false,
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
                coverConfig: { title: '', subtitle: '', author: '', date: '', logo: 'school-badge.png', logo_width: '9cm', logo_space_above: '0em', logo_space_below: '0em' },
                _saveTimer: null,
                _writerPromise: Promise.resolve(true),
                _coverSaveTimer: null,
                _coverDirty: false,
                toast: { show: false, msg: '', isError: false },

                toggleTheme() {
                    this.theme = this.theme === 'dark' ? 'light' : 'dark';
                    document.documentElement.dataset.theme = this.theme;
                    localStorage.setItem('probhub-theme', this.theme);
                },

                problemLetter(index) {
                    let value = Number(index) + 1;
                    let result = '';
                    while (value > 0) {
                        value -= 1;
                        result = String.fromCharCode(65 + (value % 26)) + result;
                        value = Math.floor(value / 26);
                    }
                    return result;
                },

                pdfPageUrl(page) {
                    return '/api/pdf-page/' + encodeURIComponent(this.currentSubtitle) + '/' + page + '?t=' + this.pdfRefresh;
                },

                difficultyBarStyle(levelIndex, color) {
                    const count = this.getDifficultyStats()[levelIndex] || 0;
                    const width = count / Math.max(1, this.problems.length) * 100;
                    return 'width:' + width + '%; background:' + color;
                },

                observeDifficultyTrack(element) {
                    this.trackWidth = element.offsetWidth;
                    new ResizeObserver(() => { this.trackWidth = element.offsetWidth; }).observe(element);
                },

                statementValue(section) {
                    const problem = this.problems[this.selectedIdx];
                    return problem && problem.statement ? (problem.statement[section] || '') : '';
                },

                sandboxInfoValue(name, fallback) {
                    const value = this.sandboxInfo ? this.sandboxInfo[name] : undefined;
                    return value == null || value === '' ? fallback : value;
                },

                sandboxLimit(name, fallback) {
                    const limits = this.sandboxInfo && this.sandboxInfo.limits;
                    const value = limits ? limits[name] : undefined;
                    return value == null ? fallback : value;
                },

                sandboxFile(name, fallback) {
                    const files = this.sandboxInfo && this.sandboxInfo.files;
                    const value = files ? files[name] : undefined;
                    return value == null || value === '' ? fallback : value;
                },

                sandboxFileCount(name) {
                    const value = this.sandboxFile(name, []);
                    return Array.isArray(value) ? value.length : 0;
                },

                submissionWorkspaceCleaned() {
                    return Boolean(this.submissionResult && this.submissionResult.submission && this.submissionResult.submission.workspace_cleaned);
                },

                submissionCleanupError() {
                    const submission = this.submissionResult && this.submissionResult.submission;
                    return submission && submission.cleanup_error ? submission.cleanup_error : '';
                },

                submissionCompileError() {
                    const compile = this.submissionCompile();
                    return compile && compile.stderr ? compile.stderr : 'compiler failed';
                },

                matrixStatus(row, program) {
                    const result = row && row.results ? row.results[program] : null;
                    return result ? result.status : undefined;
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
                            this.loadCoverage();
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
                    this.loadHealth();
                    this.loadCoverage();
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

                removeLastTag() {
                    if (this.tagDraft || this.selectedIdx === null) return;
                    const tags = this.getTags(this.selectedIdx);
                    if (tags.length) this.removeTag(this.selectedIdx, tags.length - 1);
                },

                // ── Cover config ────────────────────────────────────────────
                loadConfig() {
                    if (!this.currentSubtitle) return;
                    fetch(`/api/config/${encodeURIComponent(this.currentSubtitle)}`)
                        .then(res => res.json())
                        .then(data => {
                            if (data.success) {
                                this.coverConfig = data.config;
                                this._coverDirty = false;
                            }
                        });
                },

                autoSaveCover() {
                    clearTimeout(this._coverSaveTimer);
                    this._coverDirty = true;
                    this._coverSaveTimer = setTimeout(() => this.saveConfig(), 800);
                },

                saveConfig() {
                    clearTimeout(this._coverSaveTimer);
                    return this._queueWriter(() => this._performConfigSave());
                },

                async _performConfigSave() {
                    if (!this.currentSubtitle || !this._coverDirty) return true;
                    try {
                        const result = await this._postWriterJson(
                            `/api/config/${encodeURIComponent(this.currentSubtitle)}`,
                            this.coverConfig
                        );
                        if (result.data.success) {
                            this.coverConfig = result.data.config || this.coverConfig;
                            this._coverDirty = false;
                            return true;
                        }
                        const message = result.data.code === 'source_conflict'
                            ? '封面保存冲突：工作区已被其他会话修改，请刷新后重试。'
                            : (result.data.code === 'build_busy'
                                ? '其他 ProbHub 写操作仍在进行，请稍后重试。'
                                : (result.data.error || '封面保存失败'));
                        this.showToast(message, true, 8000);
                        return false;
                    } catch (_) {
                        this.showToast('封面保存请求失败，请确认 WebUI 服务仍在运行。', true, 8000);
                        return false;
                    }
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

                openSandbox() {
                    this.activePage = 'sandbox';
                    this.refreshSandboxInfo();
                },

                openHealth() {
                    this.activePage = 'health';
                    this.loadHealth();
                },

                openCoverage() {
                    this.activePage = 'coverage';
                    this.loadCoverage();
                },

                loadHealth() {
                    if (!this.currentSubtitle) {
                        this.health = null;
                        return;
                    }
                    const subtitle = this.currentSubtitle;
                    this.healthLoading = true;
                    this.healthError = '';
                    fetch(`/api/health?subtitle=${encodeURIComponent(subtitle)}`)
                        .then(res => res.json().then(data => ({ok: res.ok, data})))
                        .then(({ok, data}) => {
                            if (this.currentSubtitle !== subtitle) return;
                            if (!ok || !data.success) {
                                this.health = null;
                                this.healthError = data.error || '健康状态暂不可用';
                                return;
                            }
                            this.health = data;
                        })
                        .catch(() => { this.health = null; this.healthError = '健康状态请求失败'; })
                        .finally(() => { this.healthLoading = false; });
                },

                loadCoverage() {
                    if (!this.currentSubtitle) {
                        this.coverage = null;
                        return;
                    }
                    const subtitle = this.currentSubtitle;
                    this.coverageLoading = true;
                    this.coverageError = '';
                    fetch(`/api/coverage?collection=${encodeURIComponent(subtitle)}`)
                        .then(res => res.json().then(data => ({ok: res.ok, data})))
                        .then(({ok, data}) => {
                            if (this.currentSubtitle !== subtitle) return;
                            if (!ok || !data.success) {
                                this.coverage = null;
                                this.coverageError = data.error || '覆盖与交付摘要暂不可用';
                                return;
                            }
                            this.coverage = data;
                        })
                        .catch(() => {
                            this.coverage = null;
                            this.coverageError = '覆盖与交付摘要请求失败';
                        })
                        .finally(() => { this.coverageLoading = false; });
                },

                coverageProblems() {
                    return this.coverage && Array.isArray(this.coverage.problems) ? this.coverage.problems : [];
                },

                coverageGroups(item) {
                    const groups = item && item.coverage && Array.isArray(item.coverage.groups)
                        ? item.coverage.groups : [];
                    return groups;
                },

                coverageRecipes(item) {
                    const recipes = item && item.coverage && Array.isArray(item.coverage.recipes)
                        ? item.coverage.recipes : [];
                    return recipes;
                },

                coverageWrong(item) {
                    const rows = item && Array.isArray(item.wrong) ? item.wrong
                        : (item && item.coverage && Array.isArray(item.coverage.wrong) ? item.coverage.wrong : []);
                    return rows;
                },

                coverageWrongSummary(item) {
                    const rows = this.coverageWrong(item);
                    if (!rows.length) return '—';
                    const passed = rows.filter(row => ['matched', 'passed', 'ok', 'current'].includes(row && row.state)).length;
                    return `${passed}/${rows.length}`;
                },

                coverageSecretSummary(item) {
                    const summary = item && item.coverage && item.coverage.secret_summary;
                    if (summary && typeof summary === 'object') return summary;
                    const groups = this.coverageGroups(item);
                    const cases = groups.reduce((total, group) => total + this.numberValue(group && group.secret_cases), 0);
                    return {cases, input_bytes: null, answer_bytes: null};
                },

                coverageUngroupedSecretCount(item) {
                    const value = item && item.coverage ? item.coverage.ungrouped_secret_cases : null;
                    return this.numberValue(value);
                },

                coverageBytes(item) {
                    const summary = this.coverageSecretSummary(item);
                    const input = Number(summary.input_bytes);
                    const answer = Number(summary.answer_bytes);
                    if (!Number.isFinite(input) && !Number.isFinite(answer)) return '—';
                    const format = value => Number.isFinite(value) ? `${value} B` : '—';
                    return `${format(input)} / ${format(answer)}`;
                },

                coverageRecipeText(recipe) {
                    if (!recipe || typeof recipe !== 'object') return String(recipe || 'case');
                    const labels = [recipe.recipe_hash ? `recipe ${recipe.recipe_hash}` : 'recipe'];
                    if (recipe.generator) labels.push(`generator: ${recipe.generator}`);
                    if (recipe.manual === true) labels.push('manual');
                    if (Array.isArray(recipe.groups) && recipe.groups.length) labels.push(`groups: ${recipe.groups.join(', ')}`);
                    return labels.join(' · ');
                },

                coverageImpactList(impact, key) {
                    const value = impact && impact[key];
                    if (Array.isArray(value)) return value;
                    return value == null || value === '' ? [] : [String(value)];
                },

                numberValue(value) {
                    if (Array.isArray(value)) return value.length;
                    const parsed = Number(value);
                    return Number.isFinite(parsed) ? parsed : 0;
                },

                coverageRatio(value) {
                    const parsed = Number(value);
                    if (!Number.isFinite(parsed)) return '—';
                    const ratio = parsed > 1 ? parsed / 100 : parsed;
                    return `${Math.round(Math.max(0, Math.min(1, ratio)) * 100)}%`;
                },

                coveragePatternText(group) {
                    const values = group && Array.isArray(group.patterns) ? group.patterns : [];
                    return values.length ? values.join(', ') : '—';
                },

                coverageTargetText(group) {
                    const values = group && Array.isArray(group.targets) ? group.targets : [];
                    return values.length ? values.join(', ') : '—';
                },

                coverageWrongStateClass(state) {
                    if (state === true || ['matched', 'passed', 'ok', 'current'].includes(state)) return 'text-success';
                    if (state === false) return 'text-danger';
                    if (['partial', 'warning', 'pending', 'missing', 'unknown'].includes(state)) return 'text-gold';
                    return 'text-danger';
                },

                coverageStatusClass(state) {
                    if (state === true || ['passed', 'pass', 'ok', 'current', 'sealed', 'ready', 'complete', 'done'].includes(state)) {
                        return 'bg-success/15 text-success';
                    }
                    if (['warning', 'partial', 'pending', 'draft', 'missing', 'stale', 'unknown', 'not-configured'].includes(state) || state == null) {
                        return 'bg-gold/15 text-gold';
                    }
                    return 'bg-danger/15 text-danger';
                },

                coverageBoolLabel(value) {
                    if (value === true) return '通过';
                    if (value === false) return '未通过';
                    return value == null || value === '' ? '—' : String(value);
                },

                coverageQa(item) {
                    const qa = item && item.judge_qa && typeof item.judge_qa === 'object' ? item.judge_qa
                        : (item && item.coverage && item.coverage.judge_qa && typeof item.coverage.judge_qa === 'object'
                            ? item.coverage.judge_qa : {});
                    return qa;
                },

                coverageQaManualReviewCount(item) {
                    const qa = this.coverageQa(item);
                    if (Array.isArray(qa.manual_review_probes)) return qa.manual_review_probes.length;
                    if (Array.isArray(qa.probes)) return qa.probes.filter(probe => probe && probe.manual_review_required).length;
                    return qa.manual_review_required === true ? 1 : 0;
                },

                coverageImpact(item) {
                    if (item && item.impact && typeof item.impact === 'object') return item.impact;
                    return item && item.coverage && item.coverage.impact && typeof item.coverage.impact === 'object'
                        ? item.coverage.impact : {};
                },

                deliveryItemValue(item, key) {
                    if (!item || typeof item !== 'object') return null;
                    if (item[key] != null) return item[key];
                    if (key === 'sealed' || key === 'qa' || key === 'evidence' || key === 'pdf' || key === 'zip' || key === 'manifest') {
                        return item.state === 'current' || item.state === 'ready' || item.state === 'complete' || item.state === 'sealed'
                            ? true : (item.state || null);
                    }
                    return null;
                },

                deliveryRequiresText(item) {
                    const value = item && item.requires;
                    if (Array.isArray(value)) return value.join(', ');
                    if (value) return String(value);
                    return Array.isArray(item && item.stale_fields) ? item.stale_fields.join(', ') : '—';
                },

                delivery() {
                    return this.coverage && this.coverage.delivery && typeof this.coverage.delivery === 'object'
                        ? this.coverage.delivery : {};
                },

                deliveryItems() {
                    const items = this.delivery().items;
                    return Array.isArray(items) ? items : [];
                },

                deliveryRemediations() {
                    const values = this.delivery().remediations;
                    return Array.isArray(values) ? values : [];
                },

                deliveryRemediationText(remediation) {
                    if (typeof remediation === 'string') return remediation;
                    if (!remediation || typeof remediation !== 'object') return String(remediation == null ? '—' : remediation);
                    return remediation.description || remediation.action_code || '需要人工复核';
                },

                healthStateClass(state) {
                    if (['passed', 'current', 'sealed', 'not-configured'].includes(state)) return 'text-success';
                    if (['warning', 'stale', 'draft', 'missing', 'unknown'].includes(state)) return 'text-gold';
                    return 'text-danger';
                },

                healthCards(item) {
                    return [
                        {label: 'Sample', state: item.checks.sample.state},
                        {label: 'Judge', state: item.checks.judge.state},
                        {label: 'Judge QA', state: item.checks.judge_qa.state},
                        {label: 'Stress', state: item.checks.stress.state},
                        {label: 'Mutation', state: item.checks.mutation.state},
                        {label: 'Evidence', state: item.evidence.calibration.state},
                        {label: 'PDF', state: item.artifacts.pdf},
                        {label: 'ZIP', state: item.artifacts.zip},
                        {label: 'Manifest', state: item.artifacts.manifest},
                    ];
                },

                healthHeadroom(item) {
                    const accepted = item.evidence.calibration.accepted || [];
                    if (!accepted.length || accepted[0].headroom_factor == null) return '暂无本机校准余量';
                    return `${accepted[0].program || 'accepted'} · ${Number(accepted[0].headroom_factor).toFixed(2)}x TL`;
                },

                healthKillSummary(item) {
                    const kills = item.evidence.calibration.resource_kills || [];
                    if (!kills.length) return '暂无 TLE / MLE / OLE 击杀证据';
                    return kills.map(kill => `${kill.status || '?'} · ${kill.program || '-'}`).join('；');
                },

                healthRemediations(item) {
                    return (item.remediations || []).slice(0, 3);
                },

                healthCommand(remediation) {
                    return Array.isArray(remediation.command) ? remediation.command.join(' ') : '';
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
                    if (!(await this.doSave())) return;
                    const jobKey = this.sandboxKey();
                    this.sandboxRunning = true;
                    this.sandboxCancelling = false;
                    this.sandboxJobId = null;
                    this.sandboxJobKey = jobKey;
                    this.sandboxLogOpen = true;
                    fetch('/api/sandbox/run', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', 'X-ProbHub-CSRF': PROBHUB_CSRF_TOKEN },
                        body: JSON.stringify({ subtitle: this.currentSubtitle, index: this.selectedIdx })
                    }).then(res => res.json()).then(data => {
                        if (!data.success) {
                            this.sandboxRunning = false;
                            this.sandboxJobId = null;
                            const message = data.code === 'queue_full'
                                ? '评测队列已满，请稍后重试。'
                                : (data.error || 'Sandbox failed to start');
                            this.showToast(message, true);
                            return;
                        }
                        this.sandboxJobId = data.job_id;
                        this.sandboxJobKey = jobKey;
                        this.pollSandboxJob(data.job_id, jobKey);
                    }).catch(() => {
                        this.sandboxRunning = false;
                        this.sandboxJobId = null;
                        this.showToast('Sandbox failed to start', true);
                    });
                },

                cancelSandbox() {
                    if (!this.sandboxJobId || !this.sandboxRunning || this.sandboxCancelling) return;
                    this.sandboxCancelling = true;
                    fetch(`/api/sandbox/job/${this.sandboxJobId}/cancel`, {
                        method: 'POST',
                        headers: { 'X-ProbHub-CSRF': PROBHUB_CSRF_TOKEN }
                    })
                        .then(res => res.json())
                        .then(data => {
                            if (!data.success) throw new Error(data.error || 'cancel failed');
                        })
                        .catch(error => {
                            this.sandboxCancelling = false;
                            this.showToast(error.message || '取消失败', true, 6000);
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
                            if (data.status === 'queued' || data.status === 'running' || data.status === 'cancelling') {
                                this.sandboxCancelling = data.status === 'cancelling';
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
                                if (this.sandboxJobId === jobId) {
                                    this.sandboxRunning = false;
                                    this.sandboxCancelling = false;
                                    this.sandboxJobId = null;
                                }
                                if (data.status === 'success') this.showToast('Sandbox finished');
                                else if (data.status === 'cancelled') this.showToast('沙箱评测已取消');
                                else this.showToast('Sandbox found issues', true);
                                this.refreshSandboxInfo();
                            }
                        })
                        .catch(() => {
                            this.sandboxRunning = false;
                            this.sandboxCancelling = false;
                            this.sandboxJobId = null;
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
                    this.submissionJobId = null;
                    this.submissionJobKey = jobKey;
                    this.submissionResult = null;
                    this.submissionLogs = '';
                    this.submissionVerdict = 'PENDING';
                    this.submissionLogOpen = true;
                    fetch('/api/submission/run', {
                        method: 'POST',
                        headers: { 'X-ProbHub-CSRF': PROBHUB_CSRF_TOKEN },
                        body: form
                    })
                        .then(async res => ({ ok: res.ok, data: await res.json() }))
                        .then(({ ok, data }) => {
                            if (!ok || !data.success) {
                                const message = data.code === 'queue_full'
                                    ? '评测队列已满，请稍后重试。'
                                    : (data.error || 'submission failed to start');
                                throw new Error(message);
                            }
                            this.submissionJobId = data.job_id;
                            this.submissionJobKey = jobKey;
                            this.submissionCache[jobKey] = { result: null, logs: '', verdict: 'PENDING', finishedAt: '' };
                            this.pollSubmissionJob(data.job_id, jobKey);
                        })
                        .catch(error => {
                            this.submissionRunning = false;
                            this.submissionJobId = null;
                            this.submissionVerdict = 'FAIL';
                            this.showToast(error.message || '提交失败', true, 6000);
                        });
                },

                cancelSubmission() {
                    if (!this.submissionJobId || !this.submissionRunning) return;
                    this.submissionVerdict = 'CANCELLING';
                    fetch(`/api/submission/job/${this.submissionJobId}/cancel`, {
                        method: 'POST',
                        headers: { 'X-ProbHub-CSRF': PROBHUB_CSRF_TOKEN }
                    })
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
                            const verdict = data.status === 'queued'
                                ? 'QUEUED'
                                : (data.status === 'running'
                                    ? 'RUNNING'
                                    : (data.status === 'cancelling'
                                        ? 'CANCELLING'
                                        : (data.verdict || data.result?.submission?.verdict || 'PENDING')));
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
                            if (this.submissionJobId === jobId) this.submissionJobId = null;
                            const finishedAt = new Date().toLocaleTimeString('zh-CN', { hour12: false });
                            this.submissionCache[jobKey].finishedAt = finishedAt;
                            if (this.sandboxKey() === jobKey) this.submissionLastRunAt = finishedAt;
                            if (data.status === 'completed') this.showToast(`提交评测完成：${verdict}`, verdict !== 'AC');
                            else if (data.status === 'cancelled') this.showToast('提交评测已取消');
                            else this.showToast('提交评测基础设施失败', true, 6000);
                        })
                        .catch(error => {
                            this.submissionRunning = false;
                            this.submissionJobId = null;
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
                    if (this.activePage === 'health') this.loadHealth();
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
                    el.replaceChildren(sanitizeRenderedMarkdown(marked.parse(text)));
                    const problemId = this.problems[this.selectedIdx]?._id;
                    if (problemId && this.currentSubtitle) {
                        const base = new URL(`/workspace/${encodeURIComponent(problemId)}/`, window.location.origin);
                        el.querySelectorAll('img[src]').forEach(img => {
                            const source = img.getAttribute('src') || '';
                            if (/^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(source)) return;
                            const resolved = new URL(source, base);
                            const prefix = `/workspace/${encodeURIComponent(problemId)}/`;
                            if (!resolved.pathname.startsWith(prefix)) return;
                            let asset;
                            try {
                                asset = resolved.pathname.slice(prefix.length).split('/').map(decodeURIComponent);
                            } catch (_) {
                                return;
                            }
                            if (!asset.length || asset.some(part => !part || part === '.' || part === '..')) return;
                            img.src = `/api/problem-assets/${encodeURIComponent(this.currentSubtitle)}/${encodeURIComponent(problemId)}/${asset.map(encodeURIComponent).join('/')}`;
                        });
                    }
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
                    return this._queueWriter(() => this._performSave());
                },

                _queueWriter(operation) {
                    const queued = this._writerPromise.then(operation, operation);
                    this._writerPromise = queued.catch(() => false);
                    return queued;
                },

                async _postWriterJson(url, payload, retries = 4) {
                    for (let attempt = 0; ; attempt++) {
                        const response = await fetch(url, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json', 'X-ProbHub-CSRF': PROBHUB_CSRF_TOKEN },
                            body: JSON.stringify(payload)
                        });
                        let data;
                        try {
                            data = await response.json();
                        } catch (_) {
                            data = { success: false, error: `HTTP ${response.status}` };
                        }
                        if (data.code !== 'build_busy' || attempt >= retries) {
                            return { ok: response.ok, status: response.status, data };
                        }
                        await new Promise(resolve => setTimeout(resolve, 250 * (2 ** attempt)));
                    }
                },

                async _performSave() {
                    if (!this.currentSubtitle) return Promise.resolve(false);
                    const subtitle = this.currentSubtitle;
                    const payload = JSON.parse(JSON.stringify(this.problems));
                    this.saveStatus = 'saving';
                    try {
                        const { data } = await this._postWriterJson('/api/data', { subtitle, problems: payload });
                        if (data.success) {
                            if (subtitle !== this.currentSubtitle) return true;
                            if (Array.isArray(data.problems)) this._mergeSavedMetadata(data.problems);
                            this.saveStatus = 'saved';
                            setTimeout(() => { if (this.saveStatus === 'saved') this.saveStatus = ''; }, 2500);
                            return true;
                        }
                        this.saveStatus = 'error';
                        const message = data.code === 'source_conflict'
                            ? '保存冲突：题目已被其他会话修改，请刷新后重试。'
                            : (data.code === 'build_busy'
                                ? '其他 ProbHub 写操作仍在进行，请稍后重试。'
                                : (data.error || '保存失败'));
                        this.showToast(message, true, 8000);
                        return false;
                    } catch (_) {
                        this.saveStatus = 'error';
                        return false;
                    }
                },

                doSave() {
                    clearTimeout(this._saveTimer);
                    return this._queueSave();
                },

                async compilePDF() {
                    if (!this.currentSubtitle) return;
                    clearTimeout(this._saveTimer);
                    clearTimeout(this._coverSaveTimer);
                    this.isCompiling = true;
                    return this._queueWriter(async () => {
                        if (!(await this._performSave()) || !(await this._performConfigSave())) {
                            this.isCompiling = false;
                            return false;
                        }
                        try {
                            const { data } = await this._postWriterJson('/api/compile', { subtitle: this.currentSubtitle });
                            this.isCompiling = false;
                            if (data.success) {
                                this.pdfRefresh = Date.now();
                                this.loadPdfPages();
                                this.showToast(`[${this.currentSubtitle}] Typst compile OK`);
                                return true;
                            }
                            const message = data.message || data.error || 'Typst 编译失败';
                            const suggestion = data.suggestion ? `建议：${data.suggestion}` : '建议：查看终端中的 Typst 报错定位具体语法位置。';
                            this.showToast(`${message}。${suggestion}`, true, 8000);
                            return false;
                        } catch (_) {
                            this.isCompiling = false;
                            this.showToast('编译请求失败。建议：确认 ui.py 服务仍在运行，并检查终端日志。', true, 8000);
                            return false;
                        }
                    });
                },
                async distributePDFs() {
                    if (!this.currentSubtitle) return;
                    clearTimeout(this._saveTimer);
                    clearTimeout(this._coverSaveTimer);
                    this.isDistributing = true;
                    return this._queueWriter(async () => {
                        if (!(await this._performSave()) || !(await this._performConfigSave())) {
                            this.isDistributing = false;
                            return false;
                        }
                        try {
                            const { data } = await this._postWriterJson('/api/distribute', { subtitle: this.currentSubtitle });
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
                                return true;
                            }
                            this.showToast(data.error || 'Distribution failed', true);
                            return false;
                        } catch (_) {
                            this.isDistributing = false;
                            return false;
                        }
                    });
                },

                showToast(msg, isError = false, duration = 3000) {
                    this.toast = { show: true, msg, isError };
                    setTimeout(() => { this.toast.show = false; }, duration);
                }
            }));
        });
