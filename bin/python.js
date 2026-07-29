const { spawnSync } = require('child_process');

const MODULE_BOOTSTRAP = [
    'import runpy, sys',
    'root = sys.argv.pop(1)',
    'module = sys.argv.pop(1)',
    "sys.path[:] = [root] + [entry for entry in sys.path if entry not in ('', root)]",
    "runpy.run_module(module, run_name='__main__', alter_sys=True)",
].join('; ');

function pythonCandidates(env = process.env) {
    if (env.PYTHON) {
        return [{ command: env.PYTHON, prefixArgs: [], explicit: true }];
    }
    if (process.platform === 'win32') {
        return [
            { command: 'python', prefixArgs: [], explicit: false },
            { command: 'py', prefixArgs: ['-3'], explicit: false },
        ];
    }
    return [
        { command: 'python3', prefixArgs: [], explicit: false },
        { command: 'python', prefixArgs: [], explicit: false },
    ];
}

function resolvePython(env = process.env) {
    const probe = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)';
    for (const candidate of pythonCandidates(env)) {
        const result = spawnSync(
            candidate.command,
            [...candidate.prefixArgs, '-I', '-c', probe],
            { env, stdio: 'ignore', timeout: 10000 },
        );
        if (!result.error && result.status === 0) {
            return candidate;
        }
        if (candidate.explicit) {
            return null;
        }
    }
    return null;
}

function pythonEnvironment(env = process.env) {
    return {
        ...env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
    };
}

function pythonModuleArgs(sourceDir, moduleName, args = []) {
    return ['-I', '-c', MODULE_BOOTSTRAP, sourceDir, moduleName, ...args];
}

module.exports = {
    pythonCandidates,
    pythonEnvironment,
    pythonModuleArgs,
    resolvePython,
};
