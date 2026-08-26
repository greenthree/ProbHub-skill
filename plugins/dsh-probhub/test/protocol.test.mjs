import assert from 'node:assert/strict'
import { resolve } from 'node:path'
import { test } from 'node:test'

import {
  awaitProcess,
  boundedText,
  buildArgv,
  classifyResult,
  parseCoreStdout,
  PROBLEM_ID_PATTERN,
  readCollected,
  sessionWorkspace,
  validateProblemId,
} from '../src/protocol.mjs'

test('builds an argv-only invocation and preserves spaces, unicode, and option-looking IDs', () => {
  const argv = buildArgv('lint', 'C:/contest/题目 集合', '-case', 'C:/node/probhub/bin/probhub.js')
  assert.deepEqual(argv, [process.execPath, 'C:/node/probhub/bin/probhub.js', '--json', '--workspace', 'C:/contest/题目 集合', 'lint', '--', '-case'])
})

test('accepts only safe single problem identifiers', () => {
  assert.equal(validateProblemId('L02'), 'L02')
  assert.equal(validateProblemId(undefined), undefined)
  assert.equal(PROBLEM_ID_PATTERN.test('A_1.2-x'), true)
  for (const value of ['', '.', '..', 'a/b', 'a\\b', 'x'.repeat(129)]) {
    assert.throws(() => validateProblemId(value))
  }
})

test('requires one JSON object and preserves Core failure semantics', () => {
  assert.deepEqual(parseCoreStdout('{"ok":true}'), { ok: true })
  assert.throws(() => parseCoreStdout(''), /empty stdout/u)
  assert.throws(() => parseCoreStdout('[1]'), /object/u)
  assert.throws(() => parseCoreStdout('{}'), /boolean ok/u)
  assert.equal(classifyResult(0, { ok: true }), 'succeeded')
  assert.equal(classifyResult(1, { ok: false }), 'probhub-failed')
  assert.equal(classifyResult(0, { ok: false }), 'probhub-failed')
})

test('bounds collected output by bytes and keeps the tail', () => {
  const tail = boundedText('前缀'.repeat(20), 8)
  assert.ok(Buffer.byteLength(tail, 'utf8') <= 8)
  const read = readCollected({ readFrom: () => ({ text: 'x', lossy: true }) }, 10)
  assert.deepEqual(read, { text: 'x', lossy: true })
})

test('uses only an absolute Harness session cwd', () => {
  const cwd = resolve('work', '..', 'contest')
  assert.equal(sessionWorkspace({ agent: { session: { header: { cwd } } } }), cwd)
  assert.throws(() => sessionWorkspace({ agent: { session: { header: { cwd: 'relative' } } } }), /absolute/u)
})

test('does not report successful process completion if cleanup fails', async () => {
  const result = await awaitProcess({
    done: Promise.resolve({ exitCode: 0, signal: null }),
    waitForExit: async () => { throw new Error('survivor') },
  })
  assert.equal(result.outcome, undefined)
  assert.match(result.cleanupError.message, /survivor/u)
})

test('reports spawn errors separately after cleanup is confirmed', async () => {
  const result = await awaitProcess({
    done: Promise.reject(new Error('spawn failed')),
    waitForExit: async () => true,
  })
  assert.match(result.processError.message, /spawn failed/u)
})
