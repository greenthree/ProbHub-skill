import { isAbsolute, resolve } from 'node:path'

export const JSON_OUTPUT_LIMIT_BYTES = 1024 * 1024
export const STDERR_LIMIT_BYTES = 256 * 1024
export const PROBLEM_ID_PATTERN = /^[A-Za-z0-9_.-]{1,128}$/

export function errorText(error) {
  return error instanceof Error ? error.message : String(error)
}

export function boundedText(value, limit = STDERR_LIMIT_BYTES) {
  if (typeof value !== 'string' || limit <= 0) return ''
  if (Buffer.byteLength(value, 'utf8') <= limit) return value
  let result = ''
  let used = 0
  for (const character of [...value].reverse()) {
    const size = Buffer.byteLength(character, 'utf8')
    if (used + size > limit) break
    result = character + result
    used += size
  }
  return result
}

export function readCollected(reader, limit) {
  if (reader === undefined) return { text: '', lossy: false }
  const output = reader.readFrom(0)
  const text = typeof output.text === 'string' ? output.text : ''
  return {
    text,
    lossy: output.lossy === true || Buffer.byteLength(text, 'utf8') > limit,
  }
}

export async function awaitProcess(handle) {
  let outcome
  try {
    outcome = await handle.done
  } catch (error) {
    try {
      const exited = await handle.waitForExit()
      if (exited !== true) return { cleanupError: new Error('subprocess cleanup did not confirm exit') }
    } catch (cleanupError) {
      return { cleanupError }
    }
    return { processError: error }
  }
  try {
    const exited = await handle.waitForExit()
    if (exited !== true) return { cleanupError: new Error('subprocess cleanup did not confirm exit') }
  } catch (cleanupError) {
    return { cleanupError }
  }
  return { outcome }
}

export function validateProblemId(problemId) {
  if (problemId === undefined) return undefined
  if (typeof problemId !== 'string' || !PROBLEM_ID_PATTERN.test(problemId) || problemId === '.' || problemId === '..') {
    throw new TypeError('problem_id must be 1-128 letters, digits, dot, underscore, or hyphen')
  }
  return problemId
}

export function sessionWorkspace(exec) {
  const cwd = exec?.agent?.session?.header?.cwd
  if (typeof cwd !== 'string' || !isAbsolute(cwd)) {
    throw new Error('the Harness session has no absolute workspace cwd')
  }
  return resolve(cwd)
}

export function buildArgv(command, workspace, problemId, probhubBin) {
  const args = ['--json', '--workspace', workspace, command]
  if (problemId !== undefined) args.push('--', problemId)
  return [process.execPath, probhubBin, ...args]
}

export function parseCoreStdout(stdout) {
  if (typeof stdout !== 'string' || stdout.trim() === '') {
    throw new SyntaxError('ProbHub returned empty stdout; expected one JSON document')
  }
  let value
  try {
    value = JSON.parse(stdout)
  } catch (error) {
    throw new SyntaxError(`ProbHub returned no valid JSON: ${errorText(error)}`)
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new SyntaxError('ProbHub JSON result must be an object')
  }
  if (typeof value.ok !== 'boolean') {
    throw new SyntaxError('ProbHub JSON result must contain a boolean ok field')
  }
  return value
}

export function classifyResult(exitCode, result) {
  return exitCode === 0 && result?.ok !== false ? 'succeeded' : 'probhub-failed'
}
