import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import { writeFile } from 'node:fs/promises'
import pkg from './package.json' with { type: 'json' }
import fragmentSource from './manifest.fragment.json' with { type: 'json' }

/* UI-18: the Bridge Workbench plugin (HK-11 runtime assembly) — an ESM library with
   the frozen singleton set external (the shell serves those via its import map) plus
   a build-emitted manifest fragment. The shell owns chrome, router, tokens and
   preflight; design: docs/design/ui/workbench_split.md §2. */

const SINGLETONS = [
  'react',
  'react-dom',
  'react-dom/client',
  'react/jsx-runtime',
  'react-router-dom',
  'locveil-ui-kit',
]

/* The manifest fragment's static part lives in manifest.fragment.json — id, entry,
   styles, and the peer majors the shell refuses-and-surfaces on (contract:
   ManifestFragment.peers). It is DATA on purpose: the backend test suite validates
   that file + package.json's version against the pinned Workbench manifest-fragment
   schema (contracts/pins/workbench/) without running this build. Keep this function
   a pure merge — anything added to the emitted manifest goes into the JSON file, and
   `entry` / `styles` there must match the lib file names configured below. */
function emitManifestFragment(): Plugin {
  return {
    name: 'bridge-manifest-fragment',
    async writeBundle() {
      const { id, ...rest } = fragmentSource
      const fragment = { id, version: pkg.version, ...rest }
      await writeFile(
        path.resolve(__dirname, 'dist/manifest.json'),
        JSON.stringify(fragment, null, 2) + '\n'
      )
    },
  }
}

export default defineConfig({
  plugins: [react(), emitManifestFragment()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    lib: {
      entry: path.resolve(__dirname, 'src/plugin.tsx'),
      formats: ['es'],
      fileName: () => 'index.js',
      cssFileName: 'style',
    },
    rollupOptions: {
      external: SINGLETONS,
    },
    sourcemap: true,
  },
})
