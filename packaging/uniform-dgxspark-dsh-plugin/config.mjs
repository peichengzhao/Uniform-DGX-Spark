import { existsSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const ENV_KEY = /^[A-Za-z_][A-Za-z0-9_]*$/

export function defaultConfigPath() {
  if (process.platform === 'win32') {
    const base = process.env.APPDATA || join(homedir(), 'AppData', 'Roaming')
    return join(base, 'uniform-dgxspark', 'platform.env')
  }
  const base = process.env.XDG_CONFIG_HOME || join(homedir(), '.config')
  return join(base, 'uniform-dgxspark', 'platform.env')
}

export function parseEnvText(text) {
  const values = {}
  for (const rawLine of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line)
    if (!match) throw new Error(`invalid configuration line: ${rawLine}`)
    const key = match[1]
    let value = match[2]
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1)
    }
    values[key] = value
  }
  return values
}

export function readConfigFile(path) {
  if (!existsSync(path)) return {}
  return parseEnvText(readFileSync(path, 'utf8'))
}

export function normalizeBaseUrl(value) {
  let parsed
  try {
    parsed = new URL(String(value || '').trim())
  } catch {
    throw new Error('platform address is invalid; use http://host:18180 or https://your-domain')
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('platform address must use http or https')
  }
  if (parsed.username || parsed.password) throw new Error('platform address must not contain a username or password')
  if (parsed.search || parsed.hash) throw new Error('platform address must not contain a query string or fragment')
  const path = parsed.pathname.replace(/\/+$/, '')
  return `${parsed.origin}${path}`
}

export function renderEnv(values) {
  const entries = Object.entries(values).filter(([, value]) => value !== undefined && value !== '')
  for (const [key, value] of entries) {
    if (!ENV_KEY.test(key)) throw new Error(`invalid environment variable name: ${key}`)
    if (/[\r\n]/.test(String(value))) throw new Error(`${key} must not contain a newline`)
  }
  return `${entries.map(([key, value]) => `${key}=${value}`).join('\n')}\n`
}

export function redactEnv(values) {
  return Object.fromEntries(Object.entries(values).map(([key, value]) => [
    key,
    /KEY|TOKEN|SECRET|PASSWORD/i.test(key) ? '<redacted>' : value,
  ]))
}
