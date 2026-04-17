// CANONICAL PM2 CONFIG for parts-bot (Pieza Finder).
// Previously this repo had two configs (ecosystem.config.js + pm2.config.js).
// As of 2026-04-17 pre-v11 cleanup: this file is the single source of truth.
// Start/reload with:  pm2 startOrReload /opt/parts-bot/pm2.config.js
module.exports = {
  apps: [{
    name: 'parts-bot',
    script: 'server.js',
    cwd: __dirname,
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '500M',
    env: {
      NODE_ENV: 'production',
      PORT: 3002
    }
  }]
};
