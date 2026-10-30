#!/usr/bin/env node
import { chmodSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { defaultConfigPath, normalizeBaseUrl, readConfigFile, redactEnv, renderEnv } from './config.mjs'

function usage() {
  process.stdout.write(`Usage: uniform-dgxspark-configure --url URL [options]\n\n`)
  process.stdout.write('  --url URL                 LAN or VPN address, e.g. http://10.21.212.6:18180\n')
  process.stdout.write('  --fallback-url URL        optional external/VPN address used when the primary fails\n')
  process.stdout.write('  --key KEY                 platform API key (prefer --key-file)\n')
  process.stdout.write('  --key-file PATH           file containing the platform API key\n')
  process.stdout.write('  --fallback-key KEY        different key for the fallback address\n')
  process.stdout.write('  --fallback-key-file PATH  file containing the fallback key\n')
  process.stdout.write('  --config PATH             override the configuration file path\n')
  process.stdout.write('  --print-only              show a redacted configuration without writing it\n')
  process.stdout.write('  --skip-check              do not test platform connectivity\n')
  process.stdout.write('  --strict                  fail when the connectivity check fails\n')
  process.stdout.write('  --show                    show the current configuration, with secrets redacted\n')
}

function parseArgs(argv) {
  const options = {
    config: defaultConfigPath(),
    skipCheck: false,
    strict: false,
    printOnly: false,
    show: false,
  }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    const take = () => {
      index += 1
      if (index >= argv.length) throw new Error(`${arg} requires a value`)
      return argv[index]
    }
    switch (arg) {
      case '--url': options.url = take(); break
      case '--fallback-url': options.fallbackUrl = take(); break
      case '--key': options.key = take(); break
      case '--key-file': options.keyFile = take(); break
      case '--fallback-key': options.fallbackKey = take(); break
      case '--fallback-key-file': options.fallbackKeyFile = take(); break
      case '--config': options.config = take(); break
      case '--print-only': options.printOnly = true; break
      case '--skip-check': options.skipCheck = true; break
      case '--strict': options.strict = true; break
      case '--show': options.show = true; break
      case '-h':
      case '--help': options.help = true; break
      default: throw new Error(`unknown option: ${arg}`)
    }
  }
  return options
}

function readSecret(path, label) {
  const value = readFileSync(path, 'utf8').replace(/[\r\n]+$/, '')
  if (!value) throw new Error(`${label} file is empty: ${path}`)
  if (/[\r\n]/.test(value)) throw new Error(`${label} must be on one line`)
  return value
}

function writePrivateFile(path, text) {
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 })
  const temporary = `${path}.tmp-${process.pid}`
  try {
    writeFileSync(temporary, text, { encoding: 'utf8', mode: 0o600 })
    renameSync(temporary, path)
    chmodSync(path, 0o600)
  } catch (error) {
    try { rmSync(temporary, { force: true }) } catch {}
    throw error
  }
}

async function probe(baseUrl, key) {
  const endpoint = `${baseUrl}/api/v1/health`
  const response = await fetch(endpoint, {
    headers: key ? { 'X-API-Key': key } : {},
    redirect: 'manual',
    signal: AbortSignal.timeout(20_000),
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response
}

async function main() {
  let options
  try {
    options = parseArgs(process.argv.slice(2))
  } catch (error) {
    process.stderr.write(`${error.message}\n\n`)
    usage()
    process.exit(2)
  }
  if (options.help) {
    usage()
    return
  }

  if (options.show) {
    const current = readConfigFile(options.config)
    if (Object.keys(current).length === 0) {
      process.stdout.write(`No configuration found: ${options.config}\n`)
      return
    }
    process.stdout.write(`${renderEnv(redactEnv(current))}`)
    process.stdout.write(`config: ${options.config}\n`)
    return
  }

  const existing = readConfigFile(options.config)
  if (!options.url) throw new Error('--url is required')
  const url = normalizeBaseUrl(options.url)
  const fallbackUrl = options.fallbackUrl === undefined || options.fallbackUrl === ''
    ? undefined
    : normalizeBaseUrl(options.fallbackUrl)

  const key = options.keyFile
    ? readSecret(options.keyFile, 'key')
    : options.key ?? existing.UNIFORM_PLATFORM_API_KEY
  if (!key) throw new Error('--key or --key-file is required')

  let fallbackKey
  if (fallbackUrl) {
    fallbackKey = options.fallbackKeyFile
      ? readSecret(options.fallbackKeyFile, 'fallback key')
      : options.fallbackKey ?? existing.UNIFORM_PLATFORM_FALLBACK_API_KEY ?? key
  }

  const values = {
    UNIFORM_PLATFORM_URL: url,
    UNIFORM_PLATFORM_API_KEY: key,
    UNIFORM_PLATFORM_FALLBACK_URL: fallbackUrl,
    UNIFORM_PLATFORM_FALLBACK_API_KEY: fallbackKey,
  }

  if (options.printOnly) {
    process.stdout.write(`${renderEnv(redactEnv(values))}`)
    process.stdout.write(`config: ${options.config} (not written)\n`)
    return
  }

  writePrivateFile(options.config, renderEnv(values))
  process.stdout.write(`Saved: ${options.config} (permissions 0600)\n`)
  process.stdout.write(`Primary: ${url}\n`)
  if (fallbackUrl) process.stdout.write(`Fallback: ${fallbackUrl}\n`)

  if (options.skipCheck) {
    process.stdout.write('Connectivity check skipped.\n')
    return
  }

  try {
    await probe(url, key)
    process.stdout.write('Connectivity check passed.\n')
  } catch (primaryError) {
    if (fallbackUrl) {
      try {
        await probe(fallbackUrl, fallbackKey)
        process.stdout.write(`Primary failed (${primaryError.message}); fallback passed.\n`)
        return
      } catch (fallbackError) {
        process.stderr.write(`Connectivity failed: primary ${primaryError.message}; fallback ${fallbackError.message}\n`)
      }
    } else {
      process.stderr.write(`Connectivity failed: ${primaryError.message}\n`)
    }
    if (options.strict) process.exit(1)
    process.stdout.write('Configuration was saved; restart DeepSeek Harness after fixing the address or key.\n')
  }
}

main().catch(error => {
  process.stderr.write(`Configuration failed: ${error.message}\n`)
  process.exit(1)
})
