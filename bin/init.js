#!/usr/bin/env node
const fs = require('fs-extra');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

// 支持参数 --local，如果带了参数就装在当前目录，否则装在全局
const isLocal = process.argv.includes('--local');
const skillName = 'probhub';

const sourceDir = path.join(__dirname, '..');

// 安全拦截：防止在源码目录安装（本地安装时适用）
if (isLocal && sourceDir === process.cwd()) {
    console.error('\n[!] 拦截提示：不能在源码目录初始化它本身。');
    process.exit(1);
}

const baseDir = isLocal ? process.cwd() : os.homedir();
const targetDirs = [
    path.join(baseDir, '.claude', 'skills', skillName),
    path.join(baseDir, '.agents', 'skills', skillName),
];

console.log('\n🚀 正在将 ProbHub 注入到 ' + (isLocal ? '本地项目' : '全局系统') + '的 Agent Skill 库...');
targetDirs.forEach(dir => console.log('📂 目标路径: ' + dir));

const filesToCopy = ['SKILL.md', 'references', 'scripts'];

try {
    targetDirs.forEach(targetDir => {
        fs.ensureDirSync(targetDir);

        filesToCopy.forEach(item => {
            const srcPath = path.join(sourceDir, item);
            const destPath = path.join(targetDir, item);

            if (fs.existsSync(srcPath)) {
                fs.copySync(srcPath, destPath);
                console.log('  [+] 成功注入到 ' + targetDir + ': ' + item);
            }
        });
    });

    console.log('\n📦 检查 Python 运行环境...');
    try {
        execSync('pip install cyaron pypdf flask', { stdio: 'ignore' });
        console.log('  [+] 依赖安装完成 (cyaron, pypdf, flask)');
    } catch (e) {
        console.log('  [-] 依赖安装跳过，请确认本地已有 cyaron、pypdf 和 flask');
    }

    console.log('\n=======================================');
    console.log('🎉 ProbHub Skill 安装完成！');
    console.log('=======================================');
    console.log('👉 下一步操作：');
    console.log('  1. 启动 Claude Code 或兼容的 Agent 工具。');
    console.log('  2. 你可以直接使用斜杠命令强制调用（无需唤醒词）:');
    console.log('     > /probhub');
    console.log('  3. 或者直接跟它说你想出一道什么题。');
    console.log('=======================================\n');

} catch (err) {
    console.error('初始化失败:', err);
}
