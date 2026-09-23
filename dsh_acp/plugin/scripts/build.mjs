import { execFileSync } from 'node:child_process'
import { mkdir, readFile, realpath } from 'node:fs/promises'
import { isAbsolute, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const pluginRoot = resolve(fileURLToPath(new URL('..', import.meta.url)))
const sourceRootInput = process.env['MYAGENTS_DSH_SOURCE_ROOT']
if (sourceRootInput === undefined || !isAbsolute(sourceRootInput)) {
  throw new Error('MYAGENTS_DSH_SOURCE_ROOT must name the absolute stock DSH checkout')
}
const sourceRoot = await realpath(sourceRootInput)
const esbuildInput = process.env['MYAGENTS_DSH_ESBUILD_BIN']
if (esbuildInput === undefined || !isAbsolute(esbuildInput)) {
  throw new Error('MYAGENTS_DSH_ESBUILD_BIN must name the verified absolute binary')
}
const esbuild = await realpath(esbuildInput)
if (!esbuild.startsWith(`${sourceRoot}${sep}`)) {
  throw new Error('verified esbuild binary must remain inside the stock DSH checkout')
}
const outdirFlag = process.argv.indexOf('--outdir')
if (outdirFlag < 0 || process.argv[outdirFlag + 1] === undefined) {
  throw new Error('usage: build.mjs --outdir <absolute-or-relative-directory>')
}
const outdir = resolve(process.argv[outdirFlag + 1])
await mkdir(outdir, { recursive: true })

// 版本与 revision 唯一来源：runtime-contract.json（与 Python adapter、验收 gate 同源）
const contractPath = join(pluginRoot, 'runtime-contract.json')
let contract
try {
  contract = JSON.parse(await readFile(contractPath, 'utf8'))
} catch (error) {
  throw new Error(`unreadable runtime contract at ${contractPath}: ${error instanceof Error ? error.message : String(error)}`)
}
const defines = {
  __MYAGENTS_HOST_VERSION__: JSON.stringify(contract.hostVersion),
  __MYAGENTS_DSH_RUNTIME_VERSION__: JSON.stringify(contract.dshRoot.version),
  __MYAGENTS_COMPATIBILITY_REVISION__: JSON.stringify(contract.compatibilityRevision),
  __MYAGENTS_POLICY_REVISION__: JSON.stringify(contract.policyRevision),
}
const defineFlags = Object.entries(defines).map(
  ([identifier, expression]) => `--define:${identifier}=${expression}`,
)

const acpSdk = await realpath(join(
  sourceRoot,
  'apps/cli/node_modules/@agentclientprotocol/sdk/dist/acp.js',
))
execFileSync(esbuild, [
  'src/index.ts',
  '--bundle',
  '--platform=node',
  '--format=esm',
  '--target=node24',
  '--minify',
  '--charset=utf8',
  '--legal-comments=none',
  '--external:@deepseek-ai/*',
  `--alias:@agentclientprotocol/sdk=${acpSdk}`,
  ...defineFlags,
  `--outfile=${join(outdir, 'index.js')}`,
], { cwd: pluginRoot, stdio: 'inherit' })
