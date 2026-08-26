import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'

import { defineTool } from '@deepseek-ai/dsh-tools'
import {
  boundedText,
  awaitProcess,
  buildArgv,
  classifyResult,
  errorText,
  JSON_OUTPUT_LIMIT_BYTES,
  parseCoreStdout,
  readCollected,
  sessionWorkspace,
  STDERR_LIMIT_BYTES,
  validateProblemId,
} from './protocol.mjs'

export const name = 'dsh-probhub'
export const inject = ['tools', 'sandboxPolicy', 'sandbox', 'subprocess']

const require = createRequire(import.meta.url)
const PROCESS_GRACE_MS = 2000
const TOOL_TIMEOUT_MS = 120000

const OUTPUT_SCHEMA = { type: 'json' }

function adapterResult(command, workspace, problemId, fields = {}) {
  return {
    schemaVersion: 1,
    adapter: 'dsh-probhub',
    command,
    ...(workspace === undefined ? {} : { workspace }),
    ...(problemId === undefined ? {} : { problemId }),
    ...fields,
  }
}

function failed(command, workspace, problemId, code, message, fields = {}) {
  return adapterResult(command, workspace, problemId, {
    ok: false,
    classification: 'adapter-failed',
    code,
    message: boundedText(message),
    ...fields,
  })
}

function cancelled(command, workspace, problemId) {
  return adapterResult(command, workspace, problemId, {
    ok: false,
    classification: 'cancelled',
    code: 'cancelled',
    message: 'ProbHub CLI execution was cancelled',
  })
}

function resolveProbHubBin() {
  const packageJson = require.resolve('probhub/package.json')
  return join(dirname(packageJson), 'bin', 'probhub.js')
}

function renderResult(_args, value) {
  const text = JSON.stringify(value, null, 2)
  const rendered = text.length > 12000 ? `${text.slice(0, 11980)}\n... [rendered output truncated]` : text
  return [{ type: 'text', text: rendered }]
}

function presentationMeta(_args, value) {
  return {
    command: value.command,
    classification: value.classification,
    ok: value.ok,
    ...(value.code === undefined ? {} : { code: value.code }),
    ...(value.workspace === undefined ? {} : { workspace: value.workspace }),
    ...(value.problemId === undefined ? {} : { problemId: value.problemId }),
  }
}

function toolDefinition(ctx, command, description) {
  return defineTool({
    name: `probhub_${command}`,
    description,
    parameters: {
      problem_id: {
        type: 'string',
        description: 'Optional stable ProbHub problem ID. The workspace is always the current Harness session workspace.',
      },
    },
    output: {
      schema: OUTPUT_SCHEMA,
      render: renderResult,
      presentationMeta,
    },
    timeoutMs: TOOL_TIMEOUT_MS,
    async execute(args, exec) {
      const problemId = validateProblemId(args.problem_id)
      return runCore(ctx, command, problemId, exec)
    },
  })
}

function confineArgv(ctx, argv, session) {
  const policy = ctx.sandboxPolicy.resolve({ session })
  if (policy.mode === 'danger-full-access') {
    return { argv, enforcement: 'none', mode: policy.mode }
  }
  const confined = ctx.sandbox.confine(argv, policy)
  return {
    argv: confined.argv,
    enforcement: confined.enforcement,
    mode: policy.mode,
  }
}

async function runCore(ctx, command, problemId, exec) {
  let workspace
  try {
    workspace = sessionWorkspace(exec)
  } catch (error) {
    return failed(command, workspace, problemId, 'session_workspace_missing', errorText(error))
  }

  if (exec.signal.aborted) return cancelled(command, workspace, problemId)

  let argv
  try {
    argv = buildArgv(command, workspace, problemId, resolveProbHubBin())
  } catch (error) {
    return failed(command, workspace, problemId, 'probhub_binary_unavailable', errorText(error))
  }

  let confined
  try {
    confined = confineArgv(ctx, argv, exec.agent.session)
  } catch (error) {
    return failed(command, workspace, problemId, 'sandbox_unavailable', errorText(error))
  }

  try {
    const handle = ctx.subprocess.spawn({
      argv: confined.argv,
      cwd: workspace,
      stdio: {
        stdin: 'ignore',
        stdout: { maxBytes: JSON_OUTPUT_LIMIT_BYTES },
        stderr: { maxBytes: STDERR_LIMIT_BYTES },
      },
      graceMs: PROCESS_GRACE_MS,
      signal: exec.signal,
      env: {
        PYTHONDONTWRITEBYTECODE: '1',
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
      },
    })
    const lifecycle = await awaitProcess(handle)
    if (lifecycle.cleanupError !== undefined) {
      return failed(command, workspace, problemId, 'cleanup_failed', errorText(lifecycle.cleanupError))
    }
    if (lifecycle.processError !== undefined) throw lifecycle.processError
    const outcome = lifecycle.outcome
    const stdout = await readCollected(handle.collected.stdout, JSON_OUTPUT_LIMIT_BYTES)
    const stderr = await readCollected(handle.collected.stderr, STDERR_LIMIT_BYTES)

    if (exec.signal.aborted) return cancelled(command, workspace, problemId)
    if (stdout.lossy) {
      return failed(command, workspace, problemId, 'stdout_limit_exceeded', 'ProbHub JSON output exceeded the adapter limit', {
        exitCode: outcome.exitCode,
        stderr: boundedText(stderr.text),
        stderrTruncated: stderr.lossy,
        enforcement: confined.enforcement,
        sandboxMode: confined.mode,
      })
    }

    let result
    try {
      result = parseCoreStdout(stdout.text)
    } catch (error) {
      return failed(command, workspace, problemId, 'invalid_json', `ProbHub returned no valid JSON: ${errorText(error)}`, {
        exitCode: outcome.exitCode,
        stderr: boundedText(stderr.text),
        stderrTruncated: stderr.lossy,
        enforcement: confined.enforcement,
        sandboxMode: confined.mode,
      })
    }

    const classification = classifyResult(outcome.exitCode, result)
    const coreOk = classification === 'succeeded'
    return adapterResult(command, workspace, problemId, {
      ok: coreOk,
      classification,
      exitCode: outcome.exitCode,
      ...(result && typeof result === 'object' && typeof result.code === 'string' ? { code: result.code } : {}),
      stderr: boundedText(stderr.text),
      stderrTruncated: stderr.lossy,
      enforcement: confined.enforcement,
      sandboxMode: confined.mode,
      result,
    })
  } catch (error) {
    if (exec.signal.aborted) return cancelled(command, workspace, problemId)
    return failed(command, workspace, problemId, 'subprocess_failed', errorText(error))
  }
}

export function apply(ctx) {
  ctx.tools.register(toolDefinition(ctx,
    'lint',
    'Run the read-only ProbHub lint command for the current Harness workspace.',
  ))
  ctx.tools.register(toolDefinition(ctx,
    'status',
    'Read the current ProbHub status for the current Harness workspace.',
  ))
}

export {
  buildArgv,
  classifyResult,
  validateProblemId,
}
