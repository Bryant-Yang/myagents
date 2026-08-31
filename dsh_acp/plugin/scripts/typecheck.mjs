#!/usr/bin/env node

import { existsSync, readdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const pluginRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const sourceRoot = process.argv[2]
if (sourceRoot === undefined || !isAbsolute(sourceRoot)) {
  throw new Error('usage: typecheck.mjs /absolute/path/to/deepseek-harness')
}
const sourcePackage = join(sourceRoot, 'package.json')
if (!existsSync(sourcePackage)) throw new Error(`missing DSH package.json: ${sourcePackage}`)
const requireFromDsh = createRequire(pathToFileURL(sourcePackage))
const ts = requireFromDsh('typescript')
const configPath = join(sourceRoot, 'tsconfig.json')
const config = ts.readConfigFile(configPath, ts.sys.readFile)
if (config.error !== undefined) {
  throw new Error(ts.flattenDiagnosticMessageText(config.error.messageText, '\n'))
}
const parsed = ts.parseJsonConfigFileContent(config.config, ts.sys, sourceRoot, {}, configPath)
const sdkTypes = join(sourceRoot, 'apps/cli/node_modules/@agentclientprotocol/sdk/dist/acp.d.ts')
const options = {
  ...parsed.options,
  noEmit: true,
  composite: false,
  incremental: false,
  allowImportingTsExtensions: true,
  skipLibCheck: true,
  paths: {
    ...parsed.options.paths,
    '@agentclientprotocol/sdk': [sdkTypes],
  },
}
const sourceDir = join(pluginRoot, 'src')
const rootNames = readdirSync(sourceDir)
  .filter(name => name.endsWith('.ts'))
  .map(name => join(sourceDir, name))
const program = ts.createProgram({ rootNames, options })
const diagnostics = ts.getPreEmitDiagnostics(program).filter(diagnostic => {
  if (diagnostic.file === undefined) return true
  const target = resolve(diagnostic.file.fileName)
  const within = relative(pluginRoot, target)
  return within === '' || (!within.startsWith(`..${sep}`) && within !== '..')
})
if (diagnostics.length > 0) {
  for (const diagnostic of diagnostics) {
    const message = ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n')
    if (diagnostic.file === undefined || diagnostic.start === undefined) {
      console.error(`TS${diagnostic.code}: ${message}`)
      continue
    }
    const position = diagnostic.file.getLineAndCharacterOfPosition(diagnostic.start)
    console.error(`${diagnostic.file.fileName}:${position.line + 1}:${position.character + 1} TS${diagnostic.code}: ${message}`)
  }
  process.exitCode = 1
}
