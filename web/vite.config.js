import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1', port: 5173, strictPort: true,
    proxy: { '/api': process.env.VALUATION_BACKEND_URL || 'http://127.0.0.1:8000', '/health': process.env.VALUATION_BACKEND_URL || 'http://127.0.0.1:8000' },
  },
})
