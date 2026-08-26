import { access, readFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const pluginRoot = dirname(dirname(fileURLToPath(import.meta.url)))

describe('official DSH profile bundle contract', () => {
  it('publishes a built JavaScript Loader plugin and declares its patch', async () => {
    const manifest = JSON.parse(await readFile(join(pluginRoot, 'package.json'), 'utf8')) as {
      private?: boolean
      main?: string
      exports?: Record<string, unknown>
      files?: string[]
      dsh?: { bundle?: { patch?: string } }
    }

    expect(manifest.private).not.toBe(true)
    expect(manifest.main).toBe('lib/index.js')
    expect(manifest.exports?.['.']).toMatchObject({ default: './lib/index.js' })
    expect(manifest.files).toEqual(expect.arrayContaining([
      'lib/index.js',
      'cordis.patch.yml',
    ]))
    expect(manifest.dsh?.bundle?.patch).toBe('./cordis.patch.yml')
  })

  it('contributes the host through a root patch without a source executable', async () => {
    const patch = await readFile(join(pluginRoot, 'cordis.patch.yml'), 'utf8')
    expect(patch).toContain("name: '@myagents/dsh-acp-host'")
    expect(patch).toContain('id: myagents-dsh-acp-host')
    expect(patch).not.toContain('../src/')
    expect(patch).not.toContain('config/cordis.yml')
    await expect(access(join(pluginRoot, 'src', 'bin.ts'))).rejects.toThrow()
  })
})
