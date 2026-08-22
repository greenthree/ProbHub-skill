#!/usr/bin/env node
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');
const packageInfo = require('../package.json');
const {
    pythonEnvironment,
    pythonModuleArgs,
    resolvePython,
} = require('./python.js');

const isLocal = process.argv.includes('--local');
const skipPythonDeps = process.argv.includes('--skip-python-deps')
    || process.env.PROBHUB_SKIP_PYTHON_DEPS === '1';
const sourceDir = path.resolve(__dirname, '..');

function runPythonModule(python, moduleName, args, timeout, failureLabel) {
    const env = pythonEnvironment(process.env);
    const result = spawnSync(
        python.command,
        [...python.prefixArgs, ...pythonModuleArgs(sourceDir, moduleName, args)],
        { cwd: process.cwd(), env, stdio: 'inherit', timeout },
    );
    if (result.error || result.status !== 0) {
        const detail = result.error ? `: ${result.error.message}` : ` (exit ${result.status})`;
        throw new Error(`${failureLabel}${detail}`);
    }
}

function installPythonDependencies() {
    if (skipPythonDeps) {
        console.log('  [=] 已按请求跳过 Python 依赖安装');
        return;
    }
    const python = resolvePython(process.env);
    if (!python) {
        throw new Error('Supported Python was not found; use CPython 3.10-3.12 on Windows/Linux x86_64 or set PYTHON to that interpreter');
    }
    runPythonModule(
        python,
        'probhub.install_deps',
        [],
        660000,
        'Python dependency installation failed',
    );
    console.log('  [+] 已使用 ProbHub CLI 的同一 Python 和受控进程安装 SHA-256 锁定依赖');
    return python;
}

function main() {
    const currentDir = path.resolve(process.cwd());
    const relativeToSource = path.relative(sourceDir, currentDir);
    if (isLocal && (relativeToSource === '' || (!relativeToSource.startsWith('..') && !path.isAbsolute(relativeToSource)))) {
        throw new Error('不能在源码目录或其子目录初始化它本身');
    }

    const baseDir = isLocal ? path.resolve(process.cwd()) : os.homedir();
    const targetDirs = [
        path.join(baseDir, '.claude', 'skills', 'probhub'),
        path.join(baseDir, '.agents', 'skills', 'probhub'),
    ];

    console.log(`\n🚀 正在将 ProbHub 注入到${isLocal ? '本地项目' : '全局系统'}的 Agent Skill 库...`);
    targetDirs.forEach(dir => console.log(`📂 目标路径: ${dir}`));
    console.log('\n📦 检查 Python 运行环境...');
    let python = resolvePython(process.env);
    if (!python) {
        throw new Error('Supported Python was not found; use CPython 3.10-3.12 on Windows/Linux x86_64 or set PYTHON to that interpreter');
    }
    if (!skipPythonDeps) {
        python = installPythonDependencies();
    } else {
        console.log('  [=] 已按请求跳过 Python 依赖安装');
    }
    runPythonModule(
        python,
        'probhub.install_skill',
        ['--base', baseDir, '--version', packageInfo.version],
        120000,
        'ProbHub Skill publication failed',
    );

    targetDirs.forEach(targetDir => console.log(`  [+] 已完整注入到 ${targetDir}`));
    console.log('\n=======================================');
    console.log('🎉 ProbHub Skill 安装完成！');
    console.log('=======================================');
    console.log('👉 下一步操作：');
    console.log('  1. 启动 Claude Code 或兼容的 Agent 工具。');
    console.log('  2. 使用 /probhub，或直接描述你想创作的题目。');
    console.log('  3. Skill 注入不会永久注册 probhub CLI；需要命令行入口时执行 npm install -g probhub。');
    console.log('=======================================\n');
}

try {
    main();
} catch (error) {
    console.error(`初始化失败: ${error.message || error}`);
    process.exitCode = 1;
}
