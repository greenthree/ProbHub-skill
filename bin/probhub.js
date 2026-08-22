#!/usr/bin/env node
const path = require('path');
const { spawnSync } = require('child_process');
const {
    pythonEnvironment,
    pythonModuleArgs,
    resolvePython,
} = require('./python.js');

const sourceDir = path.join(__dirname, '..');
const env = pythonEnvironment(process.env);
const python = resolvePython(env);
if (!python) {
    console.error('[ProbHub] Supported Python was not found. Use CPython 3.10-3.12 on Windows/Linux x86_64, or set PYTHON to that interpreter.');
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
