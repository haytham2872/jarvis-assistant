import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: './',
  server: { port: 5173 },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        manualChunks: {
          'three':        ['three'],
          'r3f':          ['@react-three/fiber'],
          'drei':         ['@react-three/drei'],
          'postprocess':  ['@react-three/postprocessing', 'postprocessing'],
          'react':        ['react', 'react-dom'],
          'zustand':      ['zustand'],
        },
      },
    },
  },
})
