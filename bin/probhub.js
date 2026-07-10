#!/usr/bin/env node
const path = require('path');
const { spawnSync } = require('child_process');

const sourceDir = path.join(__dirname, '..');
const env = { ...process.env };
env.PYTHONPATH = env.PYTHONPATH ? `${sourceDir}${path.delimiter}${env.PYTHONPATH}` : sourceDir;
const cliArgs = ['-m', 'probhub', ...process.argv.slice(2)];
const candidates = process.env.PYTHON ? [[process.env.PYTHON, cliArgs]] : (
    process.platform === 'win32'
        ? [['python', cliArgs], ['py', ['-3', ...cliArgs]]]
        : [['python3', cliArgs], ['python', cliArgs]]
);

for (const [command, args] of candidates) {
    const result = spawnSync(command, args, { cwd: process.cwd(), env, stdio: 'inherit' });
    if (!result.error || result.error.code !== 'ENOENT') {
        process.exit(result.status === null ? 1 : result.status);
    }
}
console.error('[ProbHub] Python 3 was not found in PATH.');
process.exit(1);
