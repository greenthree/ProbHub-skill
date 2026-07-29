#!/usr/bin/env node
const path = require('path');
const { spawnSync } = require('child_process');
const { pythonModuleArgs, resolvePython } = require('./python.js');

const sourceDir = path.join(__dirname, '..');
const env = { ...process.env };
const python = resolvePython(env);
if (!python) {
    console.error('[ProbHub] Python >= 3.10 was not found. Set PYTHON to the desired interpreter.');
    process.exit(1);
}
const result = spawnSync(
    python.command,
    [
        ...python.prefixArgs,
        ...pythonModuleArgs(sourceDir, 'probhub', process.argv.slice(2)),
    ],
    { cwd: process.cwd(), env, stdio: 'inherit' },
);
process.exit(result.status === null ? 1 : result.status);
