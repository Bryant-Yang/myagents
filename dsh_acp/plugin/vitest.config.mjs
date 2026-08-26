import { dirname, isAbsolute, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const dshRoot = process.env['MYAGENTS_DSH_SOURCE_ROOT']
if (dshRoot === undefined || !isAbsolute(dshRoot)) {
  throw new Error('MYAGENTS_DSH_SOURCE_ROOT must name the DSH checkout')
}

const pluginRoot = dirname(fileURLToPath(import.meta.url))
const { default: tsconfigPaths } = await import(pathToFileURL(join(
  dshRoot,
  'node_modules/vite-tsconfig-paths/dist/index.js',
)).href)
const { default: ts } = await import(pathToFileURL(join(
  dshRoot,
  'node_modules/typescript/lib/typescript.js',
)).href)
const source = (relative) => join(dshRoot, relative)
const aliasTargets = {
  '@agentclientprotocol/sdk': source('node_modules/@agentclientprotocol/sdk/dist/acp.js'),
  '@deepseek-ai/cordis': source('vendor/cordis/src/index.ts'),
  '@deepseek-ai/schemastery': source('vendor/schemastery/src/index.ts'),
  '@deepseek-ai/dsh-agent': source('packages/core/agent/src/index.ts'),
  '@deepseek-ai/dsh-agent-loop': source('packages/core/agent-loop/src/index.ts'),
  '@deepseek-ai/dsh-agent-loop-testkit': source('packages/test-support/agent-loop-testkit/src/index.ts'),
  '@deepseek-ai/dsh-agent-default-model': source('packages/core/agent-default-model/src/index.ts'),
  '@deepseek-ai/dsh-agent-spine-demo': source('packages/examples/agent-spine-demo/src/index.ts'),
  '@deepseek-ai/dsh-app-boot': source('packages/boot/app-boot/src/index.ts'),
  '@deepseek-ai/dsh-attachment': source('packages/attachment/attachment/src/index.ts'),
  '@deepseek-ai/dsh-attachment-local': source('packages/attachment/attachment-local/src/index.ts'),
  '@deepseek-ai/dsh-credentials-local': source('packages/credentials/credentials-local/src/index.ts'),
  '@deepseek-ai/dsh-llm': source('packages/llm/llm/src/index.ts'),
  '@deepseek-ai/dsh-llm-deepseek': source('packages/llm/llm-deepseek/src/index.ts'),
  '@deepseek-ai/dsh-sandbox-policy': source('packages/sandbox/sandbox-policy/src/index.ts'),
  '@deepseek-ai/dsh-session': source('packages/core/session/src/index.ts'),
  '@deepseek-ai/dsh-session-checkpoint-policy': source('packages/session/session-checkpoint-policy/src/index.ts'),
  '@deepseek-ai/dsh-session-persistence-jsonl': source('packages/session/session-persistence-jsonl/src/index.ts'),
  '@deepseek-ai/dsh-session-query': source('packages/session-query/session-query/src/index.ts'),
  '@deepseek-ai/dsh-session-query-sqlite': source('packages/session-query/session-query-sqlite/src/index.ts'),
  '@deepseek-ai/dsh-settings-file': source('packages/settings/settings-file/src/index.ts'),
  '@deepseek-ai/dsh-tool-call-timeout-policy': source('packages/guard/timeout-policy/src/index.ts'),
  '@deepseek-ai/dsh-tool-fs': source('packages/fs/tool-fs/src/index.ts'),
  '@deepseek-ai/dsh-tool-fs-search': source('packages/fs/tool-fs-search/src/index.ts'),
  '@deepseek-ai/dsh-tools': source('packages/core/tools/src/index.ts'),
  '@deepseek-ai/dsh-user-approval': source('packages/interaction/user-approval/src/index.ts'),
}
const aliases = Object.entries(aliasTargets).map(([specifier, replacement]) => ({
  find: new RegExp(`^${specifier.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`),
  replacement,
}))
const decoratorSyntax = /^\s*@[A-Za-z_$][\w$]*/m
const standardDecoratorPlugin = () => ({
  name: 'myagents-dsh-standard-decorators',
  enforce: 'pre',
  transform(code, id) {
    const file = id.split('?', 1)[0]
    if (!/\.[cm]?tsx?$/.test(file) || !decoratorSyntax.test(code)) return undefined
    const result = ts.transpileModule(code, {
      fileName: file,
      compilerOptions: {
        target: ts.ScriptTarget.ES2024,
        module: ts.ModuleKind.ESNext,
        jsx: file.endsWith('x') ? ts.JsxEmit.ReactJSX : undefined,
        sourceMap: true,
      },
    })
    return { code: result.outputText, map: result.sourceMapText }
  },
})

export default {
  root: dshRoot,
  plugins: [
    tsconfigPaths({
      projects: [join(dshRoot, 'tsconfig.base.json')],
      loose: true,
    }),
    standardDecoratorPlugin(),
  ],
  resolve: { alias: aliases },
  test: {
    include: [join(pluginRoot, 'tests/**/*.spec.ts')],
    pool: 'forks',
    setupFiles: [join(dshRoot, 'scripts/test-invariants.ts')],
  },
}
