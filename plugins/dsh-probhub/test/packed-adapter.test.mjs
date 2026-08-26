import assert from 'node:assert/strict'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'node:test'

const installRoot = process.env.DSH_PROBHUB_PACKED_ROOT
if (!installRoot) throw new Error('DSH_PROBHUB_PACKED_ROOT must name the temporary packed-install root')

const adapter = await import(pathToFileURL(join(installRoot, 'node_modules', 'dsh-probhub', 'src', 'index.mjs')))

function outputReader(text, lossy = false) {
  return { readFrom: () => ({ text, lossy, nextOffset: Buffer.byteLength(text) }) }
}

function processHandle({ stdout = '{"ok":true}', stderr = '', exitCode = 0, stdoutLossy = false, stderrLossy = false, waitError } = {}) {
  return {
    done: Promise.resolve({ exitCode, signal: null }),
    waitForExit: async () => {
      if (waitError) throw waitError
      return true
    },
    collected: {
      stdout: outputReader(stdout, stdoutLossy),
      stderr: outputReader(stderr, stderrLossy),
    },
  }
}

function harness(overrides = {}) {
  const registered = []
  const spawns = []
  const policyRequests = []
  const confined = []
  let nextHandle = overrides.handle ?? processHandle()
  const ctx = {
    tools: { register(tool) { registered.push(tool) } },
    sandboxPolicy: {
      resolve(request) {
        policyRequests.push(request)
        return { mode: 'read-only', workspaceRoot: request.session.header.cwd, sessionId: request.session.id }
      },
    },
    sandbox: {
      confine(argv, policy) {
        confined.push({ argv, policy })
        return { argv: [...argv], enforcement: 'full', denialSignatures: [], runnerFailureRules: [] }
      },
    },
    subprocess: {
      spawn(spec) {
        spawns.push(spec)
        return typeof nextHandle === 'function' ? nextHandle() : nextHandle
      },
    },
  }
  adapter.apply(ctx)
  return { registered, spawns, policyRequests, confined, setHandle(handle) { nextHandle = handle } }
}

function execution(cwd, signal = new AbortController().signal) {
  return { signal, agent: { session: { id: 'session-1', header: { cwd } } } }
}

function workspacePath(name = 'workspace') {
  return resolve(process.cwd(), name)
}

test('registers exactly the two P0 tools and confines the installed Core CLI argv', async () => {
  const mock = harness()
  assert.deepEqual(mock.registered.map(tool => tool.name), ['probhub_lint', 'probhub_status'])
  const cwd = workspacePath('work spaces 题目')
  const result = await mock.registered[0].execute({ problem_id: 'L02' }, execution(cwd))
  assert.equal(result.classification, 'succeeded')
  assert.equal(result.workspace, cwd)
  assert.equal(mock.policyRequests[0].session.header.cwd, cwd)
  assert.equal(mock.confined.length, 1)
  assert.equal(mock.spawns.length, 1)
  assert.equal(mock.spawns[0].argv[0], process.execPath)
  assert.match(mock.spawns[0].argv[1], /[\\/]probhub[\\/]bin[\\/]probhub\.js$/u)
  assert.deepEqual(mock.spawns[0].argv.slice(2), ['--json', '--workspace', cwd, 'lint', '--', 'L02'])
  assert.equal(mock.spawns[0].cwd, cwd)
  assert.equal('shell' in mock.spawns[0], false)
})

test('does not accept a model-provided workspace parameter', async () => {
  const mock = harness()
  const cwd = workspacePath('session-root')
  const result = await mock.registered[1].execute({ workspace: workspacePath('attacker'), problem_id: 'L02' }, execution(cwd))
  assert.equal(result.workspace, cwd)
  assert.equal(mock.spawns[0].cwd, cwd)
})

test('maps abort before spawn to cancelled', async () => {
  const mock = harness()
  const controller = new AbortController()
  controller.abort()
  const result = await mock.registered[0].execute({}, execution(workspacePath(), controller.signal))
  assert.equal(result.classification, 'cancelled')
  assert.equal(mock.spawns.length, 0)
})

test('missing session cwd returns lossless adapter JSON without spawning', async () => {
  const mock = harness()
  const result = await mock.registered[0].execute({}, {
    signal: new AbortController().signal,
    agent: { session: { id: 'session-1', header: {} } },
  })
  assert.equal(result.classification, 'adapter-failed')
  assert.equal(result.code, 'session_workspace_missing')
  assert.equal('workspace' in result, false)
  assert.doesNotThrow(() => JSON.stringify(result))
  const meta = mock.registered[0].output.presentationMeta({}, result)
  assert.equal('workspace' in meta, false)
  assert.equal(mock.spawns.length, 0)
})

test('maps malformed or truncated Core stdout to adapter-failed', async () => {
  const malformed = harness({ handle: processHandle({ stdout: 'not-json' }) })
  const malformedResult = await malformed.registered[0].execute({}, execution(workspacePath()))
  assert.equal(malformedResult.classification, 'adapter-failed')
  assert.equal(malformedResult.code, 'invalid_json')

  const truncated = harness({ handle: processHandle({ stdoutLossy: true }) })
  const truncatedResult = await truncated.registered[0].execute({}, execution(workspacePath()))
  assert.equal(truncatedResult.code, 'stdout_limit_exceeded')
})

test('preserves valid Core failures and reports lossy stderr separately', async () => {
  const mock = harness({ handle: processHandle({ stdout: '{"ok":false,"code":"stale"}', stderr: 'tail', stderrLossy: true }) })
  const result = await mock.registered[1].execute({}, execution(workspacePath()))
  assert.equal(result.classification, 'probhub-failed')
  assert.equal(result.code, 'stale')
  assert.equal(result.stderr, 'tail')
  assert.equal(result.stderrTruncated, true)
})

test('cleanup failure overrides an otherwise successful Core result', async () => {
  const mock = harness({ handle: processHandle({ waitError: new Error('tree survived') }) })
  const result = await mock.registered[0].execute({}, execution(workspacePath()))
  assert.equal(result.classification, 'adapter-failed')
  assert.equal(result.code, 'cleanup_failed')
  assert.match(result.message, /tree survived/u)
})
