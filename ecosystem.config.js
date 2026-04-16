module.exports = {
  apps: [
    {
      name: 'parts-bot',
      script: 'server.js',
      cwd: '/Users/donnamolina/projects/parts-bot',
      env: {
        NODE_ENV: 'production',
        PORT: 3002,
      },
      // Restart on crash, but not if it keeps crashing (e.g. auth issue)
      max_restarts: 10,
      min_uptime: '10s',
      restart_delay: 5000,
      // Log rotation
      out_file: './logs/pm2-out.log',
      error_file: './logs/pm2-err.log',
      merge_logs: true,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      // Don't auto-start on system boot (WhatsApp auth must be present)
      autorestart: true,
    },
  ],
};
