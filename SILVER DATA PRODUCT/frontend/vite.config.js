import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/chat':   'http://localhost:8000',
      '/upload': 'http://localhost:8000',
      '/files':  'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
})
