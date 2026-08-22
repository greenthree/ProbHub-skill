const { spawnSync } = require('child_process');

const MODULE_BOOTSTRAP = [
    'import runpy, site, sys',
    'root = sys.argv.pop(1)',
    'module = sys.argv.pop(1)',
    "stdout = getattr(sys, 'stdout', None)",
    "stderr = getattr(sys, 'stderr', None)",
    "getattr(stdout, 'reconfigure', lambda **kwargs: None)(encoding='utf-8', errors='backslashreplace')",
    "getattr(stderr, 'reconfigure', lambda **kwargs: None)(encoding='utf-8', errors='backslashreplace')",
    "inside_venv = bool(getattr(sys, 'real_prefix', None) or sys.prefix != getattr(sys, 'base_prefix', sys.prefix))",
    'user_sites = [] if inside_venv else site.getusersitepackages()',
    'user_sites = [user_sites] if isinstance(user_sites, str) else list(user_sites)',
    "base_paths = [entry for entry in sys.path if entry not in ('', root) and entry not in user_sites]",
    "package_index = next((index for index, entry in enumerate(base_paths) if entry.replace('\\\\', '/').lower().endswith(('/site-packages', '/dist-packages'))), len(base_paths))",
    "sys.path[:] = [root] + base_paths[:package_index] + [entry for entry in user_sites if entry and entry != root] + base_paths[package_index:]",
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
    const probe = [
        'import platform, sys',
        'version = (sys.version_info.major, sys.version_info.minor)',
        "supported = ((3, 10) <= version < (3, 13) and sys.implementation.name == 'cpython' and platform.system() in ('Windows', 'Linux') and platform.machine().lower() in ('x86_64', 'amd64'))",
        'raise SystemExit(0 if supported else 1)',
    ].join('; ');
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
