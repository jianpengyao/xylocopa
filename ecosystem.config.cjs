// Xylocopa — pm2 process configuration
// Usage:  pm2 start ecosystem.config.cjs
//         pm2 stop   ecosystem.config.cjs
//         pm2 logs

const path = require('path');
const fs = require('fs');

const ROOT = __dirname;
// Use the venv's python directly with `-m uvicorn` instead of the `uvicorn`
// console-script. pip-installed scripts have hardcoded absolute-path shebangs
// that break when the project directory is moved; `.venv/bin/python` is a
// symlink and doesn't have that problem.
const VENV_PYTHON = path.join(ROOT, '.venv', 'bin', 'python');
const ENV_FILE = path.join(ROOT, '.env');

// Load .env into a plain object for pm2 env injection
function loadDotenv(filePath) {
  const env = {};
  if (!fs.existsSync(filePath)) return env;
  for (const line of fs.readFileSync(filePath, 'utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq < 1) continue;
    const key = trimmed.slice(0, eq).trim();
    let val = trimmed.slice(eq + 1).trim();
    // Strip surrounding quotes
    if ((val.startsWith('"') && val.endsWith('"')) ||
        (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    env[key] = val;
  }
  return env;
}

const dotenv = loadDotenv(ENV_FILE);
const port = dotenv.PORT || '8080';
const frontendPort = dotenv.FRONTEND_PORT || '3000';

module.exports = {
  apps: [
    {
      name: 'xylocopa-backend',
      cwd: path.join(ROOT, 'orchestrator'),
      script: VENV_PYTHON,
      args: `-m uvicorn main:app --host 0.0.0.0 --port ${port}`,
      interpreter: 'none',
      env: {
        ...dotenv,
        PROJECTS_DIR: dotenv.HOST_PROJECTS_DIR || path.join(require('os').homedir(), 'xylocopa-projects'),
        DB_PATH: path.join(ROOT, 'data', 'orchestrator.db'),
        LOG_DIR: path.join(ROOT, 'logs'),
        BACKUP_DIR: path.join(ROOT, 'backups'),
        PROJECT_CONFIGS_PATH: path.join(ROOT, 'project-configs'),
        XYLOCOPA_MANAGED: '1',
        AGENTHIVE_MANAGED: '1',  // legacy alias for external consumers
        // uvicorn RSS grew to ~900 MB although a gc.collect()+malloc_trim(0)
        // brought it back to ~250 MB: glibc per-thread malloc arenas holding
        // freed memory, not a leak. Two arenas keep the process compact.
        MALLOC_ARENA_MAX: '2',
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      // Safety net: if RSS grows past 8GB, pm2 graceful-restarts before
      // the kernel OOM-kills the whole user.slice (which previously took
      // down every tmux session on the host). Real steady-state is ~200MB,
      // so 8GB is comfortably above any normal usage spike.
      max_memory_restart: '8G',
      log_file: path.join(ROOT, 'logs', 'backend-pm2.log'),
      error_file: path.join(ROOT, 'logs', 'backend-pm2-error.log'),
      merge_logs: true,
      max_size: '50M',
    },
    {
      name: 'xylocopa-frontend',
      cwd: path.join(ROOT, 'frontend'),
      script: 'npx',
      args: `vite preview --host 0.0.0.0 --port ${frontendPort}`,
      interpreter: 'none',
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,
      log_file: path.join(ROOT, 'logs', 'frontend-pm2.log'),
      error_file: path.join(ROOT, 'logs', 'frontend-pm2-error.log'),
      merge_logs: true,
      max_size: '50M',
    },
  ],
};
