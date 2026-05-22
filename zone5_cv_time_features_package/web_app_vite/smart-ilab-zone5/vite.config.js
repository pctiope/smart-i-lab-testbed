import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";
import { loadEnv } from 'vite';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd());

  // Shared configure: inject API key on every proxied request
  const withApiKey = (proxy) => {
    proxy.on('proxyReq', (proxyReq) => {
      proxyReq.setHeader('x-api-key', env.VITE_API_KEY || '');
    });
  };

  return {
    plugins: [tailwindcss()],
    server: {
      proxy: {
        // Zone-5 occupancy backend (YOLO + inference stream)
        "/api": {
          target: env.VITE_INFERENCE_API_URL,
          changeOrigin: true,
          configure: withApiKey,
        },

        // Smart I-Lab IoT REST API (air-1, msr-2, sensibo, ag-one …)
        // Requests to /env-api/<path> are forwarded to http://10.158.66.30/<path>
        "/env-api": {
          target: env.VITE_ILAB_API_URL,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/env-api/, ""),
          configure: withApiKey,
        },
      },
    },
    build: {
      chunkSizeWarningLimit: 5000,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes('node_modules')) {
              return id.toString().split('node_modules/')[1].split('/')[0].toString();
            }
          }
        }
      }
    }
  };
});
