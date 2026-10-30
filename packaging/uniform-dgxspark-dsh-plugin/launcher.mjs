#!/usr/bin/env node
import { spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defaultConfigPath, readConfigFile } from './config.mjs'

const packageDir = dirname(fileURLToPath(import.meta.url))
const serverCandidates = [
  join(packageDir, 'runtime', 'uniform_harness_mcp.py'),
  join(packageDir, '..', '..', 'src', 'uniform_dgxspark', 'harness_mcp.py'),
]
const serverScript = serverCandidates.find(candidate => existsSync(candidate))
const forwardedArgs = process.argv.slice(2)

function fail(message, code = 2) {
  process.stderr.write(`${message}\n`)
  process.exit(code)
}

function findPython() {
  const candidates = []
  if (process.env.UNIFORM_PYTHON) candidates.push({ command: process.env.UNIFORM_PYTHON, args: [] })
  if (process.platform === 'win32') {
    candidates.push({ command: 'py', args: ['-3'] })
    candidates.push({ command: 'python', args: [] })
    candidates.push({ command: 'python3', args: [] })
  } else {
    candidates.push({ command: 'python3', args: [] })
    candidates.push({ command: 'python', args: [] })
    candidates.push({ command: '/usr/bin/python3', args: [] })
  }
  return candidates.find(candidate => {
    if (candidate.command === process.env.UNIFORM_PYTHON && !existsSync(candidate.command)) return false
    const checked = spawnSync(
      candidate.command,
      [...candidate.args, '-c', 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'],
      { stdio: 'ignore', windowsHide: true },
    )
    return !checked.error && checked.status === 0
  })
}

if (!serverScript) fail(`Uniform MCP runtime is missing. Checked:\n${serverCandidates.join('\n')}`, 78)

const configPath = process.env.UNIFORM_PLATFORM_CONFIG || defaultConfigPath()
let fileValues = {}
try {
  fileValues = readConfigFile(configPath)
} catch (error) {
  fail(`Cannot read Uniform configuration ${configPath}: ${error.message}`)
}

const childEnv = {
  ...process.env,
  ...fileValues,
  UNIFORM_PLATFORM_CONFIG: configPath,
  PYTHONUNBUFFERED: '1',
  PYTHONUTF8: '1',
}
const hasAddress = Boolean(childEnv.UNIFORM_PLATFORM_URL || childEnv.UNIFORM_CONTROL_URL)
if (!hasAddress && !forwardedArgs.includes('--version')) {
  fail(
    `Uniform platform is not configured.\n`
    + `Run: node "${join(packageDir, 'configure.mjs')}" --url http://YOUR-DGX-SPARK:18180 --key-file PATH_TO_KEY\n`
    + `Configuration file: ${configPath}`,
  )
}

const python = findPython()
if (!python) {
  fail(
    'Python 3.10+ was not found on this computer.\n'
    + 'Install Python 3, or set UNIFORM_PYTHON to its executable path, then retry.',
    69,
  )
}

const child = spawn(
  python.command,
  [...python.args, '-u', serverScript, ...forwardedArgs],
  { env: childEnv, stdio: 'inherit', windowsHide: true },
)

for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(signal, () => {
    if (!child.killed) child.kill(signal)
  })
}

child.on('error', error => fail(`Cannot start the Uniform MCP server: ${error.message}`, 70))
child.on('exit', (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal)
    return
  }
  process.exit(code ?? 1)
})
