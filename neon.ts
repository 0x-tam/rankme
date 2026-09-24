import { defineConfig } from '@neon/config/v1';

export default defineConfig({
  auth: true,
  preview: {
    // Upgrade to a paid plan to enable AI Gateway for your project.
    // aiGateway: true,
    buckets: {
      uploads: {
        access: 'private',
      },
    },
    functions: {
      api: {
        name: 'api',
        source: './cloud/index.ts',
        env: {
          RANKME_PUBLIC_ORIGIN: 'https://getrankme.vercel.app',
          RANKME_COOKIE_SECRET: process.env.RANKME_COOKIE_SECRET!,
          RANKME_WORKER_TOKEN: process.env.RANKME_WORKER_TOKEN!,
        },
      },
    },
  },
});
