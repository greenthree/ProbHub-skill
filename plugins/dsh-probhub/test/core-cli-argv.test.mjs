import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, symlinkSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { spawnSync } from 'node:child_process'
import { test } from 'node:test'

const installRoot = process.env.DSH_PROBHUB_PACKED_ROOT
const workspace = process.env.DSH_PROBHUB_WORKSPACE
if (!installRoot || !workspace) {
  throw new Error('DSH_PROBHUB_PACKED_ROOT and DSH_PROBHUB_WORKSPACE are required')
}

const bin = join(installRoot, 'node_modules', 'probhub', 'bin', 'probhub.js')

function run(workspacePath, args) {
  const result = spawnSync(process.execPath, [bin, '--json', '--workspace', workspacePath, 'lint', ...args], {
    encoding: 'utf8',
    env: {
      ...process.env,
      PROBHUB_ALLOW_SYSTEM_PYTHON: '1',
      PYTHONDONTWRITEBYTECODE: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
    },
    maxBuffer: 8 * 1024 * 1024,
    shell: false,
  })
  assert.equal(result.error, undefined)
  return { ...result, value: JSON.parse(result.stdout) }
}

test('current Core parser accepts no ID, one ID, and multiple IDs after --', () => {
  const all = run(workspace, [])
  assert.equal(all.status, 0)
  assert.equal(all.value.ok, true)

  const one = run(workspace, ['--', 'L02'])
  assert.equal(one.status, 0)
  assert.deepEqual(one.value.problems.map(problem => problem.id), ['L02'])

  const many = run(workspace, ['--', 'L02', 'L03'])
  assert.equal(many.status, 0)
  assert.deepEqual(many.value.problems.map(problem => problem.id).sort(), ['L02', 'L03'])
})

test('current Core parser treats an option-looking ID after -- as an ID', () => {
  const result = run(workspace, ['--', '-missing'])
  assert.notEqual(result.status, 0)
  assert.equal(result.value.ok, false)
  assert.match(result.value.error, /unknown problem id/u)
})

test('current Core parser preserves a workspace path containing spaces and Unicode', () => {
  const root = mkdtempSync(join(tmpdir(), 'probhub 题目 '))
  const alias = join(root, '工作区 alias')
  try {
    symlinkSync(workspace, alias, process.platform === 'win32' ? 'junction' : 'dir')
    const result = run(alias, ['--', 'L02'])
    assert.equal(result.status, 0)
    assert.deepEqual(result.value.problems.map(problem => problem.id), ['L02'])
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})
