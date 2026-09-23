import { execFileSync } from 'node:child_process'
import { mkdir, readFile, realpath } from 'node:fs/promises'
import { isAbsolute, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const pluginRoot = resolve(fileURLToPath(new URL('..', import.meta.url)))
// 双模式：源码模式（MYAGENTS_DSH_SOURCE_ROOT，release gate 路径）要求
// esbuild 位于 checkout 内；installed 模式（MYAGENTS_DSH_CLI）从 npm 安装
// 的官方 dsh 解析 acp-sdk alias，esbuild 允许显式指定的任意绝对路径——
// 构建工具差异会改变产物字节，最终被契约 entrySha256 捕获。
const sourceRootInput = process.env['MYAGENTS_DSH_SOURCE_ROOT']
const cliInput = process.env['MYAGENTS_DSH_CLI']
let dshRoot
let acpSdkAliasSource
if (sourceRootInput !== undefined) {
  if (!isAbsolute(sourceRootInput)) {
    throw new Error('MYAGENTS_DSH_SOURCE_ROOT must name the absolute stock DSH checkout')
  }
  dshRoot = await realpath(sourceRootInput)
  acpSdkAliasSource = join(
    dshRoot,
    'apps/cli/node_modules/@agentclientprotocol/sdk/dist/acp.js',
  )
} else if (cliInput !== undefined && isAbsolute(cliInput)) {
  const cliEntry = await realpath(cliInput)
  dshRoot = resolve(cliEntry, '..', '..')
  acpSdkAliasSource = join(
    dshRoot, 'node_modules/@agentclientprotocol/sdk/dist/acp.js')
} else {
  throw new Error(
    'MYAGENTS_DSH_SOURCE_ROOT（源码模式）或 MYAGENTS_DSH_CLI'
    + '（installed 模式）必须指定一个绝对路径')
}
const esbuildInput = process.env['MYAGENTS_DSH_ESBUILD_BIN']
if (esbuildInput === undefined || !isAbsolute(esbuildInput)) {
  throw new Error('MYAGENTS_DSH_ESBUILD_BIN must name the verified absolute binary')
}
const esbuild = await realpath(esbuildInput)
if (sourceRootInput !== undefined && !esbuild.startsWith(`${dshRoot}${sep}`)) {
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

const acpSdk = await realpath(acpSdkAliasSource)
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
