import productionConfig from '../frontend/vite.config.ts'

export default {
  ...productionConfig,
  server: { ...productionConfig.server, proxy: { '/api': { target: 'http://127.0.0.1:8001', ws: true } } },
}
